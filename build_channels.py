#!/usr/bin/env python3
"""
EPG automático a partir da sua playlist M3U, usando os raspadores do iptv-org/epg.

  python build_channels.py build <url-ou-arquivo.m3u>
      -> gera channels.xml com TODAS as fontes candidatas de cada canal,
         idmap.json e faltando.txt (canais ao vivo sem nenhuma fonte)

  python build_channels.py rename raw.xml guide.xml.gz
      -> para cada canal usa a primeira fonte (por prioridade) que trouxe
         programação, troca os ids pelos tvg-id da playlist e compacta.
         Gera relatorio.txt (fonte usada por canal) e sem_programacao.txt
"""
import copy
import gzip
import json
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# Ordem de prioridade: a primeira fonte que trouxer programação vence.
SITES = [
    "claro.com.br",
    "vivoplay.com.br",
    "mi.tv",
    "clarotvmais.com.br",
    "meuguia.tv",
    "guiadetv.com",
]
EPG_DIR = Path("epg")            # clone do github.com/iptv-org/epg
MAP_FILE = Path("idmap.json")

VOD_URL = re.compile(r"/(movie|movies|series)/|\.(mp4|mkv|avi|mov)(\?|$)", re.I)
VOD_NAME = re.compile(r"\bS\d{1,2}\s?E\d{1,3}\b", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[\[\(].*?[\]\)]", " ", s)
    s = re.sub(r"\b(fhd|uhd|hd|sd|4k|h265|hevc|alt|backup)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def read_m3u(src: str) -> list[dict]:
    if src.startswith("http"):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=300).read().decode("utf-8", "ignore")
    else:
        text = Path(src).read_text("utf-8", "ignore")

    seen, chans, pending, vod = set(), [], None, 0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            pending = line
            continue
        if not line or line.startswith("#") or pending is None:
            continue
        extinf, url, pending = pending, line, None

        tid = re.search(r'tvg-id="([^"]*)"', extinf)
        tname = re.search(r'tvg-name="([^"]*)"', extinf)
        title = extinf.rsplit(",", 1)[-1].strip()
        tvg_id = tid.group(1).strip() if tid else ""
        name = tname.group(1).strip() if tname and tname.group(1).strip() else title

        if VOD_URL.search(url) or VOD_NAME.search(name):
            vod += 1
            continue
        out_id = tvg_id or name
        if out_id and out_id not in seen:
            seen.add(out_id)
            chans.append({"tvg_id": tvg_id, "name": name, "out_id": out_id})
    print(f"playlist: {len(chans)} canais ao vivo ({vod} itens de filme/série ignorados)")
    return chans


def load_sites() -> dict:
    sites = {}
    for site in SITES:
        f = EPG_DIR / "sites" / site / f"{site}.channels.xml"
        if not f.exists():
            print(f"aviso: {f} não encontrado, pulando")
            continue
        by_id, by_name = {}, {}
        for ch in ET.parse(f).getroot().iter("channel"):
            xid = (ch.get("xmltv_id") or "").lower()
            if xid:
                by_id.setdefault(xid, ch)
                by_id.setdefault(norm(xid.split(".")[0]), ch)
            by_name.setdefault(norm(ch.text), ch)
        sites[site] = (by_id, by_name)
    return sites


def find_all(info: dict, sites: dict):
    """Todas as fontes que têm o canal, em ordem de prioridade."""
    keys_id = [info["tvg_id"].lower(), norm(info["tvg_id"])] if info["tvg_id"] else []
    keys_name = [norm(info["name"]), norm(info["tvg_id"])]
    hits = []
    for site, (by_id, by_name) in sites.items():
        ch = next((by_id[k] for k in keys_id if k and k in by_id), None)
        if ch is None:
            ch = next((by_name[k] for k in keys_name if k and k in by_name), None)
        if ch is not None:
            hits.append((site, ch))
    return hits


def build(src: str):
    playlist = read_m3u(src)
    sites = load_sites()

    root = ET.Element("channels")
    internal_of = {}      # (site, site_id) -> id interno
    idmap, missing = [], []

    for info in playlist:
        hits = find_all(info, sites)
        if not hits:
            missing.append(info["name"])
            continue
        candidates = []
        for site, ch in hits:
            key = (site, ch.get("site_id"))
            if key not in internal_of:
                internal = f"ch{len(internal_of):05d}"
                internal_of[key] = internal
                el = ET.SubElement(root, "channel", {
                    "site": site,
                    "lang": ch.get("lang", "pt"),
                    "xmltv_id": internal,
                    "site_id": ch.get("site_id"),
                })
                el.text = ch.text
            candidates.append({"internal": internal_of[key], "site": site})
        idmap.append({"id": info["out_id"], "name": info["name"], "candidates": candidates})

    ET.indent(root)
    ET.ElementTree(root).write("channels.xml", encoding="utf-8", xml_declaration=True)
    MAP_FILE.write_text(json.dumps(idmap, ensure_ascii=False, indent=1), "utf-8")
    Path("faltando.txt").write_text("\n".join(sorted(missing)) + "\n", "utf-8")

    per_site = defaultdict(int)
    for site, _ in internal_of:
        per_site[site] += 1
    print(f"{len(idmap)}/{len(playlist)} canais com pelo menos uma fonte; "
          f"{len(missing)} em faltando.txt")
    print("grades a baixar por fonte:", dict(per_site))


def rename(raw: str, out: str):
    idmap = json.loads(MAP_FILE.read_text("utf-8"))
    opener = gzip.open if raw.endswith(".gz") else open
    with opener(raw, "rb") as f:
        src = ET.parse(f).getroot()

    chans = {c.get("id"): c for c in src.findall("channel")}
    progs = defaultdict(list)
    for p in src.findall("programme"):
        progs[p.get("channel")].append(p)

    tv = ET.Element("tv", {"generator-info-name": "epg-auto"})
    out_progs, report, empty = [], [], []
    site_ok = defaultdict(int)

    for entry in idmap:
        chosen = next((c for c in entry["candidates"] if progs.get(c["internal"])), None)
        if chosen is None:
            tried = ", ".join(c["site"] for c in entry["candidates"])
            empty.append(f'{entry["name"]}  (tentou: {tried})')
            continue
        site_ok[chosen["site"]] += 1
        report.append(f'{entry["name"]}  ->  {chosen["site"]}')

        c = copy.deepcopy(chans.get(chosen["internal"], ET.Element("channel")))
        c.tag = "channel"
        c.set("id", entry["id"])
        for dn in c.findall("display-name"):
            c.remove(dn)
        dn = ET.Element("display-name", {"lang": "pt"})
        dn.text = entry["name"]
        c.insert(0, dn)
        tv.append(c)

        for p in progs[chosen["internal"]]:
            np_ = copy.deepcopy(p)
            np_.set("channel", entry["id"])
            out_progs.append(np_)

    tv.extend(out_progs)
    with gzip.open(out, "wb") as f:
        f.write(ET.tostring(tv, encoding="utf-8", xml_declaration=True))

    Path("relatorio.txt").write_text("\n".join(sorted(report)) + "\n", "utf-8")
    Path("sem_programacao.txt").write_text("\n".join(sorted(empty)) + "\n", "utf-8")
    print(f"{out}: {len(report)} canais com programação, {len(out_progs)} programas")
    print(f"{len(empty)} canais sem programação em nenhuma fonte (sem_programacao.txt)")
    print("fontes que funcionaram:", dict(site_ok))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "build":
        build(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "rename":
        rename(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)
