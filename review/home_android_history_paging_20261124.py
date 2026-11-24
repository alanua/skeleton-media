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
    if(!page.optBoolean("has_more",false))return null
    val actualOffset=page.optInt("offset",-1)
    val count=reply.optJSONArray("items")?.length() ?: 0
    if(actualOffset!=expectedOffset || count<=0 || count>100 || expectedOffset<0){
        throw IllegalStateException("Некоректна сторінка історії")
    }
    val next=expectedOffset+count
    if(next>10_000)throw IllegalStateException("Історія перевищує ліміт безпечного читання")
    return next
}

private suspend fun savedVideoHistoryItemsPaged(api:HomeApi):List<JSONObject>{
    val collected=mutableListOf<JSONObject>()
    var offset=0
    repeat(100){
        currentCoroutineContext().ensureActive()
        val path=if(offset==0)savedVideoHistoryPath() else "${savedVideoHistoryPath()}?offset=$offset&limit=100"
        val response=api.get(path)
        currentCoroutineContext().ensureActive()
        collected.addAll(response.optJSONArray("items")?.objects().orEmpty())
        val next=nextSavedVideoHistoryOffset(response,offset)
        if(next==null)return canonicalHistoryItems(collected)
        offset=next
    }
    throw IllegalStateException("Історія не завершила завантаження")
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
    modified=original
    for old,new in ((OLD,NEW),(OLD_QUERY,NEW_QUERY),(OLD_COUNTER,NEW_COUNTER)):
        if modified.count(old)!=1:
            raise ValueError("ANDROID_PATCH_ANCHOR_MISMATCH")
        modified=modified.replace(old,new,1)
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
