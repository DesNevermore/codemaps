from __future__ import annotations
import re
try:
 from .scenario_generators import CapabilityProfile
except ImportError:
 from scenario_generators import CapabilityProfile

def _active_evidence(lines:list[str], nouns:tuple[str,...], verbs:tuple[str,...])->bool:
 for line in lines:
  if re.search(r"\b(files?|extensions?)\s+(detected|classified)\s+as\b",line):
   continue
  if any(n in line for n in nouns) and any(v in line for v in verbs):
   return True
 return False


def detect_capabilities(help_text:str,readme:str='')->CapabilityProfile:
 t=(help_text+'\n'+readme).lower();lines=[x.strip() for x in t.splitlines() if x.strip()]
 inputs=set();ops=set();outputs=set();state=set()
 if any(x in t for x in ('file','directory','recursive','path')):inputs.add('directory_tree');state.add('filesystem')
 if (any(x in t for x in ('markdown','chapter')) or
     _active_evidence(lines,('document','documentation','html'),
                      ('parse','render','convert','generate','build','extract','format','input'))):
  inputs.add('structured_document')
 if (any(x in t for x in ('abstract syntax tree','syntax tree')) or re.search(r'\bast\b',t) or
     _active_evidence(lines,('source code','programming language','code file'),
                      ('parse','lint','rewrite','format','search','match','transform','analy'))):
  inputs.add('source_code')
 if (any(x in t for x in ('sample rate','sample-rate','audio channels')) or
     _active_evidence(lines,('audio','wav','sound'),
                      ('convert','encode','decode','mix','play','filter','resample','record','process'))):
  inputs.add('audio');outputs.add('audio')
 if _active_evidence(lines,('url','website','web site','http','link'),
                     ('crawl','fetch','check','serve','request','download','upload','visit')):
  inputs.add('url');state.add('http')
 if (any(x in t for x in ('git repository','patch stack')) or
     _active_evidence(lines,('repository','commit','branch','patch'),
                      ('create','edit','apply','push','pop','rebase','clone','manage','inspect'))):
  inputs.add('repository')
 if any(x in t for x in ('format','pretty','render','build','compile')):ops.add('transform')
 if any(x in t for x in ('sort','count','total','summary')):ops.add('aggregate')
 if any(x in t for x in ('output file','output directory','export','build directory','generate')):outputs.add('artifact_tree')
 if any(x in t for x in ('watch','server','serve')):state.add('long_running')
 if ('curses' in t or 'terminal user interface' in t or 'tui' in t or 'alternate screen' in t or
     any(x in t for x in ('key bindings','keyboard controls','terminal resize','full-screen terminal')) or
     ('interactive' in t and any(x in t for x in ('terminal screen','keyboard','key bindings','curses'))) or
     _active_evidence(lines,('terminal','tty','screen'),
                      ('display','draw','redraw','keypress','keyboard','resize','screensaver','rebound'))):
  state.add('terminal')
 return CapabilityProfile(frozenset(inputs),frozenset(ops),frozenset(outputs),frozenset(state))
