"""SHA-pinned source-only Android history pagination patch; no APK deployment."""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path

ANDROID_SHA = "fdbfbd108bebeb5bd6a20c4ddd57d658f5ff3152f98db72f6ad4b731a06473a4"

OLD = """private suspend fun savedVideoHistoryItemsWithRetry(api:HomeApi,attempts:Int=2):List<JSONObject>{
    var lastError:Throwable?=null
    repeat(attempts.coerceAtLeast(1)){i->
        val result=runCatching{api.get(savedVideoHistoryPath()).optJSONArray("items")?.objects().orEmpty()}
        result.onSuccess{return it}
        result.exceptionOrNull()?.let{if(it is kotlinx.coroutines.CancellationException)throw it}
        lastError=result.exceptionOrNull()
        if(i<attempts-1)delay(650)
    }
    throw lastError ?: IllegalStateException("Історія недоступна")
}"""

NEW = """// Server with no page field is accepted as a legacy single-page response.
internal fun nextSavedVideoHistoryOffset(reply:JSONObject,expectedOffset:Int):Int?{
    val page=reply.optJSONObject("page") ?: return null
    if(page.optBoolean("restart_required",false)){
        throw IllegalStateException("Медіатека змінилася. Оновлюємо список.")
    }
    val hasMore=page.optBoolean("has_more",false)
    val actualOffset=page.optInt("offset",-1)
    val count=reply.optJSONArray("items")?.length() ?: 0
    if(actualOffset!=expectedOffset || (hasMore && count<=0) || count>100 || expectedOffset<0 ||
       expectedOffset>Int.MAX_VALUE-count || page.optInt("returned",-1)!=count){
        throw IllegalStateException("Некоректна сторінка історії")
    }
    val next=expectedOffset+count
    val matching=page.optInt("matching",-1)
    if(matching<next || hasMore!=(next<matching)){
        throw IllegalStateException("Неповна сторінка історії")
    }
    return if(hasMore)next else null
}

private suspend fun savedVideoHistoryItemsPaged(api:HomeApi):List<JSONObject>{
    val collected=mutableListOf<JSONObject>()
    var offset=0
    var revision:String?=null
    while(true){
        currentCoroutineContext().ensureActive()
        val path=if(offset==0)savedVideoHistoryPath() else "${savedVideoHistoryPath()}?offset=$offset&limit=100&revision=$revision"
        val response=api.get(path)
        currentCoroutineContext().ensureActive()
        val rows=response.optJSONArray("items")?.objects().orEmpty()
        val page=response.optJSONObject("page")
        if(page==null){
            if(offset!=0 || rows.size>=100)throw IllegalStateException("Сервер ще не підтримує повну медіатеку")
            return canonicalHistoryItems(rows)
        }
        val next=nextSavedVideoHistoryOffset(response,offset)
        val pageRevision=page.optString("revision")
        if(!pageRevision.matches(Regex("[0-9a-f]{64}")) || (revision!=null && revision!=pageRevision)){
            throw IllegalStateException("Медіатека змінилася. Оновлюємо список.")
        }
        revision=pageRevision
        val counts=response.optJSONObject("counts") ?: throw IllegalStateException("Лічильники медіатеки недоступні")
        val total=counts.optInt("total",-1)
        val visible=counts.optInt("visible",-1)
        if(total!=page.optInt("matching",-1) || visible !in 0..total || counts.optInt("hidden",-1)!=total-visible){
            throw IllegalStateException("Некоректні лічильники медіатеки")
        }
        collected.addAll(rows)
        if(next==null){
            val canonical=canonicalHistoryItems(collected)
            if(canonical.size!=total || canonical.count{!it.optBoolean("hidden",false)}!=visible){
                throw IllegalStateException("Неповна медіатека")
            }
            return canonical
        }
        offset=next
    }
}
private suspend fun savedVideoHistoryItemsWithRetry(api:HomeApi,attempts:Int=2):List<JSONObject>{
    var lastError:Throwable?=null
    repeat(attempts.coerceAtLeast(1)){i->
        val result=runCatching{savedVideoHistoryItemsPaged(api)}
        result.onSuccess{return it}
        result.exceptionOrNull()?.let{if(it is kotlinx.coroutines.CancellationException)throw it}
        lastError=result.exceptionOrNull()
        if(i<attempts-1)delay(650)
    }
    throw lastError ?: IllegalStateException("Історія недоступна")
}"""

OLD_COUNTER='Text(if(q.isBlank())"${items.size}" else "${visible.size} з ${items.size}",fontSize=10.sp,fontWeight=FontWeight.Bold,color=Accent)'
NEW_COUNTER='Text(if(q.isBlank())"$totalCount/$visibleCount" else "${visible.size} з $totalCount/$visibleCount",fontSize=10.sp,fontWeight=FontWeight.Bold,color=Accent)'
OLD_QUERY='    val q=query.trim().lowercase(Locale.ROOT)'
NEW_QUERY='    val q=query.trim().lowercase(Locale.ROOT)\n    val totalCount=items.size\n    val visibleCount=items.count{!it.optBoolean("hidden",false)}'

def patch_candidate(original: str, *, expected_sha: str=ANDROID_SHA) -> str:
    if hashlib.sha256(original.encode()).hexdigest()!=expected_sha:
        raise ValueError("ANDROID_SOURCE_PARITY_FAILED")
    old_open='        val open=api.post("/api/video/history/open",savedHistoryOpenBody(endpoint.endpointId,item,seasonHint))'
    new_open=old_open+"""
        // Do not spend up to 15 seconds polling a work with no full source.
        if(open.optString("status") in setOf("discovering","no_verified_sources","searching_sources")){
            throw IllegalStateException("Повне відеоджерело ще не знайдено. Пошук триває у фоні.")
        }"""
    modified=original
    for old,new in ((OLD,NEW),(OLD_COUNTER,NEW_COUNTER),(old_open,new_open)):
        if modified.count(old)!=1:
            raise ValueError("ANDROID_PATCH_ANCHOR_MISMATCH")
        modified=modified.replace(old,new,1)
    # More than one screen defines the same 'val q='. Only the search box
    # immediately preceding the editor's unique total/visible counter is ours.
    editor_counter=modified.index(NEW_COUNTER)
    search_at=modified.rfind(OLD_QUERY,0,editor_counter)
    if search_at<0 or editor_counter-search_at>3500:
        raise ValueError("ANDROID_HISTORY_EDITOR_QUERY_NOT_FOUND")
    modified=modified[:search_at]+NEW_QUERY+modified[search_at+len(OLD_QUERY):]
    return modified

def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--input",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    src,dest=args.input.resolve(),args.output.resolve()
    if src==dest or dest.exists() or not str(dest).startswith("/tmp/"):
        raise ValueError("ISOLATED_OUTPUT_REQUIRED")
    changed=patch_candidate(src.read_text(encoding="utf-8"))
    dest.write_text(changed,encoding="utf-8")
    dest.chmod(0o600)
    print("SOURCE_ONLY_ANDROID_PATCH=true")
    print("INPUT_SHA="+ANDROID_SHA)
    print("OUTPUT_SHA="+hashlib.sha256(changed.encode()).hexdigest())
    print("APK_UNCHANGED=true")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
