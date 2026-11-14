"""Display one location when object and section carry the same name."""
import re


def location(object_name,section_name,separator=' · '):
    clean=lambda value:re.sub(r'\s+',' ',str(value or '')).strip()
    normalize=lambda value:re.sub(r'\s*[-–—]\s*','-',clean(value)).casefold()
    first=clean(object_name);second=clean(section_name)
    return first if not second or normalize(first)==normalize(second) else separator.join(p for p in (first,second) if p)


def section_button_title(section):
    """Keep the section identity visible in a single mobile button line."""
    clean=lambda value:' '.join(str(value or '').split())
    place=location(section.project_object.name,section.name)
    rnu=clean(section.rnu)
    rnu=re.sub(r'\bНовосибирское\s+РНУ\b','НСК',rnu,flags=re.I)
    rnu=re.sub(r'\bОмское\s+РНУ\b','Омск',rnu,flags=re.I)
    parts=[place,rnu]
    pipeline=clean(section.pipeline)
    if pipeline and pipeline.casefold() not in place.casefold():parts.append(pipeline)
    return ' · '.join(clean(p) for p in parts if p)[:100]
