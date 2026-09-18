import resolver


def test_balanced_json_value_handles_nested_strings():
    text='x seasons:[{"season":1,"episodes":[{"episode":"1","title":"a ] b"}]}], tail'
    start=text.index('[')
    assert resolver._balanced_json_value(text,start).endswith(']}]')


def test_zenith_sources_parses_seasons(monkeypatch):
    embed='https://api.zenithjs.ws/embed/kp/325787?oneSound=ukr'
    page='https://lavakino.net/serialy/20000-inspektor-mors.html'
    body='''makePlayer({playlist:{seasons:[{"season":1,"blocked":false,"episodes":[{"episode":"1","hls":"https://cdn.example.com/a/master.m3u8?t=1","audio":{"names":["Українська","English"]},"duration":123,"title":"Morse S1E1"}]}]}});'''
    monkeypatch.setattr(resolver,'_curl_text',lambda *a,**k: body)
    monkeypatch.setattr(resolver.site_registry,'validate_public_url',lambda u:(u,'cdn.example.com'))
    rows=resolver._zenith_sources(embed,'Lavakino','Серіал',0,page)
    assert len(rows)==1
    assert rows[0]['season']=='1'
    assert rows[0]['episode']=='Серія 1'
    assert rows[0]['translation']=='Українська / English'
    assert rows[0]['url'].endswith('master.m3u8?t=1')
    assert rows[0]['headers']['Referer']==embed
