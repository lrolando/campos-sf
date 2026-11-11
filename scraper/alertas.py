"""Lee las alertas por email de Zonaprop y Argenprop desde Gmail (IMAP, solo lectura).

No se conecta a Zonaprop ni a Argenprop: toma los avisos de los emails que esos sitios te mandan.

Uso de prueba:
    python scraper/alertas.py --test             (lee Gmail y muestra lo que encuentra; necesita las variables de entorno)
    python scraper/alertas.py --eml alerta.eml   (prueba con un email guardado desde Gmail: "Descargar mensaje")

Variables de entorno: GMAIL_USER y GMAIL_APP_PASSWORD (contraseña de aplicación de Google).
"""
import email, imaplib, os, re, sys
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

from bs4 import BeautifulSoup

PATRONES = {
    "Zonaprop": re.compile(r"https?://(?:www\.)?zonaprop\.com\.ar/propiedades/[^\s\"'<>?#&]+?-(\d{6,})\.html", re.I),
    "Argenprop": re.compile(r"https?://(?:www\.)?argenprop\.com/[^\s\"'<>?#&/]+?--(\d{6,})", re.I),
}
RE_PRECIO = re.compile(r"(?:USD|U\$S|US\$|\$)\s*\d[\d\.\,]*", re.I)
MESES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def resolver(href):
    """Encuentra la URL real del aviso, aunque venga dentro de un link de seguimiento
    (http://click.../?url=https%3A%2F%2Fwww.zonaprop...). Devuelve (fuente, url, codigo) o None."""
    s = href or ""
    for _ in range(3):
        for fuente, pat in PATRONES.items():
            m = pat.search(s)
            if m:
                return fuente, m.group(0).replace("http://", "https://"), m.group(1)
        s2 = unquote(s)
        if s2 == s:
            break
        s = s2
    return None


def avisos_de_html(html, fecha):
    soup = BeautifulSoup(html, "html.parser")
    enlaces = [(a, resolver(a.get("href"))) for a in soup.find_all("a", href=True)]
    enlaces = [(a, r) for a, r in enlaces if r]
    por_codigo = {}
    for a, (fuente, url, cod) in enlaces:
        por_codigo.setdefault((fuente, cod), {"fuente": fuente, "url": url, "links": []})["links"].append(a)

    out = []
    for (fuente, cod), d in por_codigo.items():
        # Subir desde el link hasta el bloque más grande que sea solo de este aviso (la "tarjeta" del email).
        caja = d["links"][0]
        for p in d["links"][0].parents:
            if p.name in ("body", "html", "[document]"):
                break
            codigos = {r[2] for a in p.find_all("a", href=True) if (r := resolver(a.get("href")))}
            if len(codigos) > 1:
                break
            caja = p
        texto = caja.get_text(" ", strip=True)
        titulos = [a.get_text(" ", strip=True) for a in d["links"]] + \
                  [img.get("alt", "") for a in d["links"] for img in a.find_all("img")]
        titulos = [t for t in titulos if len(t) > 8 and not RE_PRECIO.fullmatch(t)
                   and not t.lower().startswith(("ver ", "http"))]
        precios = RE_PRECIO.findall(texto)
        usd = [p for p in precios if not p.startswith("$")]
        slug = re.sub(r"[-/]+", " ", d["url"].split("/")[-1].rsplit("-", 1)[0]).strip()
        slug = re.sub(r"^vecl\w*\s+", "", slug)  # prefijo interno de Zonaprop (veclcain, veclapin...)
        out.append({
            "fuente": fuente, "url": d["url"], "codigo": cod, "fecha": fecha,
            "titulo": max(titulos, key=len) if titulos else slug.capitalize(),
            "precio_txt": (usd or precios or [""])[0],
            "slug": slug, "texto": texto[:1500],
        })
    return out


def avisos_de_mensaje(msg):
    try:
        fecha = parsedate_to_datetime(msg["Date"]).date().isoformat()
    except Exception:
        fecha = date.today().isoformat()
    out = []
    for parte in msg.walk():
        tipo = parte.get_content_type()
        if tipo not in ("text/html", "text/plain"):
            continue
        cuerpo = parte.get_payload(decode=True)
        if not cuerpo:
            continue
        texto = cuerpo.decode(parte.get_content_charset() or "utf-8", errors="replace")
        if tipo == "text/plain":  # sin HTML: convertir las URLs sueltas en links
            texto = re.sub(r"(https?://\S+)", r'<a href="\1">\1</a>', texto)
        for x in avisos_de_html(texto, fecha):
            x["html"] = tipo == "text/html"
            out.append(x)
    vistos = {}
    for a in sorted(out, key=lambda x: not x["html"]):  # mismo aviso en la parte HTML y en la de texto: gana HTML
        vistos.setdefault((a["fuente"], a["codigo"]), a)
    return list(vistos.values())


def leer_gmail(cfg):
    """Devuelve (avisos crudos, cantidad de emails leídos). Lanza excepción si falla la conexión."""
    usuario, clave = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not usuario or not clave:
        raise RuntimeError("faltan GMAIL_USER / GMAIL_APP_PASSWORD")
    desde = date.today() - timedelta(days=cfg.get("dias_hacia_atras", 10))
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(usuario, clave.replace(" ", ""))
        estado, _ = imap.select('"%s"' % cfg.get("etiqueta", "alertas-inmuebles"), readonly=True)
        if estado != "OK":
            raise RuntimeError(f"no existe la etiqueta '{cfg.get('etiqueta')}' en Gmail")
        _, datos = imap.search(None, "SINCE", f"{desde.day:02d}-{MESES[desde.month - 1]}-{desde.year}")
        ids = datos[0].split()
        avisos = []
        for i in ids:
            _, partes = imap.fetch(i, "(BODY.PEEK[])")  # PEEK: no marca los emails como leídos
            avisos += avisos_de_mensaje(email.message_from_bytes(partes[0][1]))
        return avisos, len(ids)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _mostrar(avisos):
    for a in avisos:
        print(f"- [{a['fuente']}] {a['fecha']} | {a['titulo'][:70]} | {a['precio_txt'] or 'sin precio'}\n  {a['url']}")
    print(f"{len(avisos)} avisos encontrados.")


if __name__ == "__main__":
    if "--eml" in sys.argv:
        ruta = sys.argv[sys.argv.index("--eml") + 1]
        with open(ruta, "rb") as f:
            _mostrar(avisos_de_mensaje(email.message_from_bytes(f.read())))
    elif "--test" in sys.argv:
        import json
        from pathlib import Path
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
        avisos, n = leer_gmail(cfg.get("alertas", {}))
        print(f"{n} emails leídos.")
        _mostrar(avisos)
    else:
        print(__doc__)
