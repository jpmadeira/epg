#!/usr/bin/env python3
"""
EPG automático a partir da sua playlist M3U, usando os raspadores do iptv-org/epg
e o banco de canais/logos do iptv-org/database.

  python build_channels.py build <url-ou-arquivo.m3u>
      -> channels.xml (todas as fontes candidatas por canal), idmap.json, faltando.txt

  python build_channels.py rename raw.xml guide.xml.gz
      -> escolhe por canal a 1ª fonte que trouxe programação, aplica tvg-id e logos.
         Gera relatorio.txt e sem_programacao.txt
"""
import copy
import csv
import difflib
import gzip
import json
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# (site, padrão do arquivo de canais) em ordem de prioridade.
# A primeira fonte que trouxer programação para o canal vence.
SITES = [
    ("claro.com.br",       "claro.com.br.channels.xml"),
    ("vivoplay.com.br",    "vivoplay.com.br.channels.xml"),
    ("mi.tv",              "mi.tv_br.channels.xml"),
    ("epgshare01.online",  "epgshare01.online_BR1.channels.xml"),
    ("clarotvmais.com.br", "clarotvmais.com.br.channels.xml"),
    ("meuguia.tv",         "meuguia.tv.channels.xml"),
    ("guiadetv.com",       "guiadetv.com.channels.xml"),
]
EPG_DIR = Path("epg")    # clone do github.com/iptv-org/epg
DB_DIR = Path("db")      # clone do github.com/iptv-org/database
MAP_FILE = Path("idmap.json")
MAX_CANDIDATES = 3       # fontes tentadas por canal (mais = mais cobertura e mais tempo)

# "playlist" = usa tvg-logo da playlist e só completa com o iptv-org;
# "iptv-org" = prefere o logo do iptv-org e usa o da playlist como reserva.
LOGO_PRIORITY = "playlist"

# Redes com afiliadas regionais: se "Globo NSC Joinville" não existir em
# nenhuma fonte, usa a grade da rede nacional.
NETWORKS = ["globo", "band", "record", "sbt", "redetv", "tvcultura", "cultura", "redevida", "tvaparecida"]
SISTERS = ("globonews", "globoplay", "bandnews", "bandsports", "recordnews", "sbtnews")

# Apelidos comuns (normalizados): nome da playlist -> nome nas fontes
ALIASES = {
    "espn1": "espn", "sportv1": "sportv", "premiere1": "premiere", "premiereclubes": "premiere",
    "discoveryhh": "discoveryhomehealth", "discoveryhomeandhealth": "discoveryhomehealth",
    "history": "historychannel", "thehistory": "historychannel", "cnbcbrasil": "timesbrasil",
    "hbo1": "hbo", "tcaction": "telecineaction", "tcpipoca": "telecinepipoca",
    "tcpremium": "telecinepremium", "tcfun": "telecinefun", "tctouch": "telecinetouch",
    "tccult": "telecinecult", "natgeo": "nationalgeographic", "nationalgeo": "nationalgeographic",
    "ae": "ae", "tnts": "tntseries", "warner": "warnerchannel", "globonacional": "globo",
    "recordtv": "record", "tvbrasil": "tvbrasil", "redeglobo": "globo", "tvglobo": "globo",
}

VOD_URL = re.compile(r"/(movie|movies|series)/|\.(mp4|mkv|avi|mov)(\?|$)", re.I)
VOD_NAME = re.compile(r"\bS\d{1,2}(\s?E\d{1,3})?\s*$|\bS\d{1,2}\s?E\d{1,3}\b", re.I)
QUALITY = {"fhd", "uhd", "hd", "sd", "4k", "8k", "hdr", "h265", "hevc", "alt", "backup",
           "60fps", "30fps", "ppv", "vip"}
FILLER = {"tv", "canal", "rede", "the", "channel", "and", "e"}


def ascii_lower(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def tokens(s: str) -> list[str]:
    s = ascii_lower(s)
    s = re.sub(r"^[^/]{2,40}/[a-z]{2}\s+", "", s)     # "São Paulo/SP Globo" -> "globo"
    s = re.sub(r"[\[\(].*?[\]\)]", " ", s)
    s = re.sub(r"plus\s*\+", "plus", s).replace("&", " ").replace("+", " plus ")
    return [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in QUALITY]


def norm(s: str) -> str:
    return "".join(tokens(s))


def keys_for(name: str) -> list[str]:
    t = tokens(name)
    out = ["".join(t)]
    core = [x for x in t if x not in FILLER]
    # sem "tv/canal/rede": só quando sobra algo distintivo (evita "Rede Brasil" == "Canal Brasil")
    if len(core) >= 2 or (core and len(core[0]) >= 7):
        out.append("".join(core))
    # afiliadas da Globo aparecem sem o prefixo nas fontes: "Globo NSC Joinville" -> "nsc joinville"
    if len(core) > 1 and core[0] == "globo" and core[1] not in ("news", "plus", "play", "playnovelas"):
        out.append("".join(t[1:]))        # "tv morena"
        out.append("".join(core[1:]))     # "morena" / "nsc joinville"
    out += [ALIASES[k] for k in list(out) if k in ALIASES]
    seen = []
    for k in out:
        if k and k not in seen:
            seen.append(k)
    return seen


def id_keys(xid: str) -> list[str]:
    if not xid:
        return []
    x = xid.lower().split("@")[0]
    return [x, norm(x.split(".")[0])]


# ---------------------------------------------------------------- playlist
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

        attr = dict(re.findall(r'([\w-]+)="([^"]*)"', extinf))
        title = extinf.rsplit(",", 1)[-1].strip()
        tvg_id = attr.get("tvg-id", "").strip()
        name = attr.get("tvg-name", "").strip() or title
        if VOD_URL.search(url) or VOD_NAME.search(name):
            vod += 1
            continue
        out_id = tvg_id or name
        if out_id and out_id not in seen:
            seen.add(out_id)
            chans.append({"tvg_id": tvg_id, "name": name, "out_id": out_id,
                          "logo": attr.get("tvg-logo", "").strip()})
    print(f"playlist: {len(chans)} canais ao vivo ({vod} itens de filme/série ignorados)")
    return chans


# ---------------------------------------------------------------- iptv-org database
def load_db():
    """nome/alt_name normalizado -> id iptv-org (BR primeiro), e id -> logo."""
    name_to_id, logos = {}, {}
    f = DB_DIR / "data" / "channels.csv"
    if f.exists():
        rows = list(csv.DictReader(f.open(encoding="utf-8")))
        rows.sort(key=lambda r: r["country"] != "BR")          # BR tem preferência
        for r in rows:
            if r.get("closed"):
                continue
            for n in [r["name"], *filter(None, r["alt_names"].split(";"))]:
                for k in keys_for(n):
                    name_to_id.setdefault(k, r["id"])
    f = DB_DIR / "data" / "logos.csv"
    if f.exists():
        best = {}
        for r in csv.DictReader(f.open(encoding="utf-8")):
            if r.get("in_use", "TRUE").upper() != "TRUE" or r.get("feed"):
                continue
            score = (r.get("format") in ("PNG", "SVG"), int(r.get("width") or 0))
            if r["channel"] not in best or score > best[r["channel"]][0]:
                best[r["channel"]] = (score, r["url"])
        logos = {k: v[1] for k, v in best.items()}
    print(f"banco iptv-org: {len(name_to_id)} nomes, {len(logos)} logos")
    return name_to_id, logos


# ---------------------------------------------------------------- fontes
def load_sites() -> dict:
    sites = {}
    for site, pattern in SITES:
        files = list((EPG_DIR / "sites" / site).glob(pattern))
        if not files:
            print(f"aviso: {site}/{pattern} não encontrado, pulando")
            continue
        idx = {"id": {}, "sid": {}, "name": {}}
        for f in files:
            for ch in ET.parse(f).getroot().iter("channel"):
                for k in id_keys(ch.get("xmltv_id")):
                    idx["id"].setdefault(k, ch)
                idx["sid"].setdefault((ch.get("site_id") or "").lower(), ch)
                for k in keys_for(ch.text):
                    idx["name"].setdefault(k, ch)
        sites[site] = idx
        print(f"fonte {site}: {len(idx['sid'])} canais")
    return sites


def regional_base(k: str):
    if k.startswith(SISTERS):
        return None
    for net in NETWORKS:
        if k.startswith(net) and k != net and not k.startswith(net + "plus"):
            return net
    return None


def lookup(idx, info, db_id, fuzzy):
    tid = info["tvg_id"]
    if tid:
        ch = idx["sid"].get(tid.lower())
        if ch is not None:
            return ch, "id"
        for k in id_keys(tid):
            if k in idx["id"]:
                return idx["id"][k], "id"
    for k in id_keys(db_id):
        if k in idx["id"]:
            return idx["id"][k], "id"
    names = keys_for(info["name"]) + (keys_for(tid) if tid and "#" not in tid else [])
    for k in names:
        if k in idx["name"]:
            return idx["name"][k], "nome"
    if fuzzy:
        k = names[0] if names else ""
        pool = [n for n in idx["name"] if n[:3] == k[:3]]
        m = difflib.get_close_matches(k, pool, n=1, cutoff=0.88)
        if m:
            return idx["name"][m[0]], "aproximado"
    return None, None


def find_all(info, sites, name_to_id):
    db_id = next((name_to_id[k] for k in keys_for(info["name"]) if k in name_to_id), "")
    info["db_id"] = db_id
    hits = []
    for fuzzy in (False, True):
        for site, idx in sites.items():
            ch, how = lookup(idx, info, db_id, fuzzy)
            if ch is not None:
                hits.append((site, ch, how))
        if hits:
            return hits
    # afiliada regional -> grade da rede nacional
    base = regional_base(norm(info["name"]))
    if base:
        for site, idx in sites.items():
            ch = next((idx["name"][k] for k in (base, base + "tv", "tv" + base) if k in idx["name"]), None)
            if ch is not None:
                hits.append((site, ch, f"rede {base}"))
    return hits


def logo_for(info, hits, name_to_id, logos):
    """Logo do iptv-org: pelo tvg-id, pelo nome, pelo canal da fonte ou pela rede."""
    for xid in (info["tvg_id"], info.get("db_id", "")):
        if xid and xid.split("@")[0] in logos:
            return logos[xid.split("@")[0]]
    for site, ch, how in hits:
        x = (ch.get("xmltv_id") or "").split("@")[0]
        if x in logos:
            return logos[x]
        for k in keys_for(ch.text):
            if k in name_to_id and name_to_id[k] in logos:
                return logos[name_to_id[k]]
    return ""


def build(src: str):
    playlist = read_m3u(src)
    name_to_id, logos = load_db()
    sites = load_sites()

    root = ET.Element("channels")
    internal_of, idmap, missing = {}, [], []
    how_count = defaultdict(int)

    for info in playlist:
        hits = find_all(info, sites, name_to_id)
        db_logo = logo_for(info, hits, name_to_id, logos)
        logo = (info["logo"] or db_logo) if LOGO_PRIORITY == "playlist" else (db_logo or info["logo"])
        if not hits:
            missing.append(info["name"])
            continue
        hits = hits[:MAX_CANDIDATES]
        how_count[hits[0][2].split()[0]] += 1
        cands = []
        for site, ch, how in hits:
            key = (site, ch.get("site_id"))
            if key not in internal_of:
                internal_of[key] = f"ch{len(internal_of):05d}"
                el = ET.SubElement(root, "channel", {
                    "site": site, "lang": ch.get("lang", "pt"),
                    "xmltv_id": internal_of[key], "site_id": ch.get("site_id"),
                })
                el.text = ch.text
            cands.append({"internal": internal_of[key], "site": site,
                          "src_name": ch.text, "how": how})
        idmap.append({"id": info["out_id"], "name": info["name"], "logo": logo, "candidates": cands})

    ET.indent(root)
    ET.ElementTree(root).write("channels.xml", encoding="utf-8", xml_declaration=True)
    MAP_FILE.write_text(json.dumps(idmap, ensure_ascii=False, indent=1), "utf-8")
    Path("faltando.txt").write_text("\n".join(sorted(missing)) + "\n", "utf-8")
    print(f"{len(idmap)}/{len(playlist)} canais com fonte ({dict(how_count)}); "
          f"{len(missing)} em faltando.txt; {len(internal_of)} grades a baixar")


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
    with_logo = 0

    for e in idmap:
        chosen = next((c for c in e["candidates"] if progs.get(c["internal"])), None)
        if chosen is None:
            tried = ", ".join(c["site"] for c in e["candidates"])
            empty.append(f'{e["name"]}  (tentou: {tried})')
            continue
        site_ok[chosen["site"]] += 1
        report.append(f'{e["name"]}  ->  {chosen["site"]}: {chosen["src_name"]} [{chosen["how"]}]')

        base = chans.get(chosen["internal"])
        c = ET.Element("channel", {"id": e["id"]})
        dn = ET.SubElement(c, "display-name", {"lang": "pt"})
        dn.text = e["name"]
        logo = e.get("logo") or (base.find("icon").get("src") if base is not None and base.find("icon") is not None else "")
        if logo:
            ET.SubElement(c, "icon", {"src": logo})
            with_logo += 1
        tv.append(c)

        for p in progs[chosen["internal"]]:
            np_ = copy.deepcopy(p)
            np_.set("channel", e["id"])
            out_progs.append(np_)

    tv.extend(out_progs)
    with gzip.open(out, "wb") as f:
        f.write(ET.tostring(tv, encoding="utf-8", xml_declaration=True))

    Path("relatorio.txt").write_text("\n".join(sorted(report)) + "\n", "utf-8")
    Path("sem_programacao.txt").write_text("\n".join(sorted(empty)) + "\n", "utf-8")
    print(f"{out}: {len(report)} canais com programação ({with_logo} com logo), {len(out_progs)} programas")
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
