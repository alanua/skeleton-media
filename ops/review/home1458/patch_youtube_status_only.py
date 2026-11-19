"""Apply reviewed observation changes while preserving recovery byte-for-byte."""
import ast
from pathlib import Path
from patch_youtube_health import patch as patch_health


def function(text,name):
    node=next(n for n in ast.parse(text).body if isinstance(n,ast.FunctionDef) and n.name==name)
    return node,ast.get_source_segment(text,node)


def patch(root,js):
    root=Path(root);p=root/'home-edge-youtube-stability';_,original=function(p.read_text(),'recover')
    patch_health(root,js)
    source=p.read_text();node,_=function(source,'recover');lines=source.splitlines(keepends=True)
    source=''.join(lines[:node.lineno-1])+original+'\n'+''.join(lines[node.end_lineno:])
    assert function(source,'recover')[1]==original
    p.write_text(source)


if __name__=='__main__':
    import sys
    patch(sys.argv[1],sys.argv[2])
