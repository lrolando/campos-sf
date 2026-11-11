"""Busca campos y casas en venta, filtra por departamento y actualiza docs/data/listings.json.
Uso:  python scraper/main.py          (corrida normal)
      python scraper/main.py --test   (solo muestra cuántos avisos encuentra cada sitio, sin guardar)
"""
import hashlib, json, os, re, sys, time, unicodedata
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "data" / "listings.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; campos-sf-bot/1.0; uso personal)",
           "Accept-Language": "es-AR,es;q=0.9"}
PRECIO_MINIMO = 1000  # por debajo es un placeholder ("USD 1", "$ 65") o un precio por ha: se muestra "Consultar"
REINTENTOS = 3

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def norm(s):
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


# localidad normalizada -> departamento
LOCALIDADES = {}
for depto, locs in CFG["departamentos"].items():
    for l in locs:
        LOCALIDADES.setdefault(norm(l), depto)
    LOCALIDADES.setdefault(norm(depto), depto)
LOCALIDADES_ORDEN = sorted(LOCALIDADES, key=len, reverse=True)  # nombres largos primero


def departamento_de(texto):
    t = " " + re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", norm(texto))) + " "
    for loc in LOCALIDADES_ORDEN:
        if f" {loc} " in t:
            return LOCALIDADES[loc], loc
    return None, None


def parse_precio(txt):
    txt = txt or ""
    moneda = "USD" if re.search(r"USD|U\$S|US\$", txt, re.I) else "ARS"
    m = re.search(r"(\d[\d\.\,]*)", txt)
    if not m:
        return None, moneda
    n = re.sub(r"[^\d]", "", m.group(1))
    precio = int(n) if n else None
    if precio is not None and precio < PRECIO_MINIMO:
        precio = None
    return precio, moneda


def parse_hectareas(txt):
    m = re.search(r"(\d[\d\.,]*)\s*(?:has?\b|hect[aá]reas?)", txt or "", re.I)
    if not m:
        # Algunos sitios dan la superficie en m² ("160000.0 m²"): desde 1 ha se convierte.
        m2 = re.search(r"(\d[\d\.,]*)\s*m(?:²|2)\b", txt or "", re.I)
        if not m2:
            return None
        n = parse_hectareas(m2.group(1) + " ha")
        return round(n / 10000, 2) if n and n >= 10000 else None
    raw = m.group(1)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") == 1 and len(raw.split(".")[1]) == 3:
        raw = raw.replace(".", "")  # 1.200 -> 1200
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def id_de(url):
    # Mercado Libre publica el mismo aviso bajo varios subdominios: se usa el código MLA para no duplicar.
    m = re.search(r"\bMLA-?(\d+)", url)
    zp = re.search(r"zonaprop\.com\.ar/.*-(\d{6,})\.html", url)
    ap = re.search(r"argenprop\.com/.*--(\d{6,})", url)
    clave = f"MLA{m.group(1)}" if m else f"ZP{zp.group(1)}" if zp else f"AP{ap.group(1)}" if ap else url
    return hashlib.sha1(clave.encode()).hexdigest()[:12]


def tipo_real(tipo, url, titulo):
    """Corrige el tipo: las búsquedas de 'campos' a veces traen casas o departamentos.
    Devuelve None si el aviso no es ni campo ni casa (departamentos, locales, etc.)."""
    sub = re.match(r"https?://([a-z]+)\.mercadolibre", url or "")
    sub = sub.group(1) if sub else ""
    if sub == "departamento":
        return None
    if sub == "casa":
        return "casa"
    t = norm(titulo)
    if tipo == "campo" and re.match(r"(venta (de )?)?(casa|duplex|chalet)\b", t) and not re.search(r"\bcampo\b|\bha\b|hectarea", t):
        return "casa"
    if re.match(r"(venta (de )?)?(departamento|depto|monoambiente|local|oficina|cochera)\b", t):
        return None
    return tipo


def sel_text(el, css):
    if isinstance(css, list):  # varios selectores: se unen sus textos ("casa • venta" + "Garibaldi 856")
        return " ".join(t for t in (sel_text(el, c) for c in css) if t)
    if css and "@" in css:  # "selector@atributo": lee un atributo (p. ej. el title completo de un enlace)
        css, attr = css.rsplit("@", 1)
        n = el.select_one(css)
        return " ".join((n.get(attr) or "").split()) if n else ""
    n = el.select_one(css) if css else None
    return " ".join(n.get_text(" ", strip=True).split()) if n else ""


def descargar(url, site):
    ultimo = None
    for intento in range(REINTENTOS):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code in (401, 403, 404):  # bloqueo o URL inexistente: no tiene sentido reintentar
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.text
        except Exception as e:
            ultimo = e
            if "HTTP 40" in str(e):
                break
            time.sleep(5 * (intento + 1))
    raise RuntimeError(f"{url}: {ultimo}")


def grupo(regex, texto):
    """Primer grupo de `regex` en `texto` (o todo el match si no tiene grupos); None si no hay match."""
    m = re.search(regex, texto or "", re.I)
    if not m:
        return None
    return (m.group(1) if m.groups() else m.group(0)).strip()


def leer_tarjeta(site, it, url):
    """Arma el aviso crudo de una tarjeta del listado, o None si hay que descartarla."""
    for basura in it.select(site["quitar"]) if site.get("quitar") else []:
        basura.decompose()  # p. ej. el código del aviso dentro del recuadro del precio
    a = it.select_one(site["link"]) if site.get("link") else it.find("a", href=True)
    if not a or not a.get("href"):
        return None
    texto = " ".join(it.get_text(" ", strip=True).split())
    if site.get("solo_si") and not re.search(site["solo_si"], texto, re.I):
        return None  # p. ej. listados que mezclan venta y alquiler
    titulo = sel_text(it, site.get("titulo")) or a.get_text(" ", strip=True)
    if site.get("excluir_si") and re.search(site["excluir_si"], titulo, re.I):
        return None  # p. ej. "VENDIDO" en el título
    ubicacion = site.get("ubicacion_fija") or sel_text(it, site.get("ubicacion"))
    if site.get("ubicacion_regex"):
        ubicacion = grupo(site["ubicacion_regex"], ubicacion) or ubicacion
    href = urljoin(url, a["href"]).split("#")[0]
    raw = {"url": href if site.get("conservar_query") else href.split("?")[0],
           "titulo": titulo, "ubicacion": ubicacion,
           "precio_txt": sel_text(it, site.get("precio")), "texto": texto}
    if site.get("tipo_regex"):  # el tipo sale del título ("Mitre 4050 | Casa en Venta")
        raw["tipo"] = tipo_de_texto(grupo(site["tipo_regex"], titulo) or "")
        if not raw["tipo"]:
            return None
    return raw


def completar_con_detalle(site, raw):
    """Entra a la página del aviso para sacar precio y/o tipo cuando el listado no los muestra.
    Devuelve False si el aviso no es campo ni casa."""
    det = site["detalle"]
    soup = BeautifulSoup(descargar(raw["url"], site), "html.parser")
    for x in soup(["script", "style", "nav", "header", "footer"]):
        x.decompose()
    texto = " ".join(soup.get_text(" ", strip=True).split())
    if det.get("precio_regex"):
        raw["precio_txt"] = grupo(det["precio_regex"], texto) or ""
    if det.get("tipo_regex"):
        raw["tipo"] = tipo_de_texto(grupo(det["tipo_regex"], texto) or "")
        if not raw["tipo"]:
            return False
    if det.get("texto_regex"):  # parte de la página que se usa para buscar hectáreas
        raw["texto"] += " " + (grupo(det["texto_regex"], texto) or "")
    return True


def scrape_api_gaggiotti(site):
    """Gaggiotti arma su sitio con JavaScript: se lee la misma API de búsqueda que usa su web
    (solo lectura, autorizado por el dueño del proyecto). Devuelve (avisos crudos, errores)."""
    tipos = {i: tipo for tipo, ids in site["tipos_api"].items() for i in ids}
    props, errores = [], []
    # Una consulta por tipo: mezclar los tipos "Campo" (100/101) con los demás devuelve 0 resultados.
    for ids in site["tipos_api"].values():
        try:
            r = SESSION.post(site["urls"][0], timeout=40, json={
                "operationTypes": [1], "propertyTypes": ids, "limit": 500, "offset": 0})
            r.raise_for_status()
            props += r.json()["response"]["properties"]
        except Exception as e:
            errores.append(f"{site['urls'][0]} {ids}: {e}")
        time.sleep(2)
    resultados = []
    for p in props:
        tipo = tipos.get((p.get("type") or {}).get("id"))
        venta = next((o for o in p.get("operations", []) if o.get("operationType") == "Compra-Venta"), None)
        partes = [x.strip() for x in (p.get("location") or {}).get("fullLocation", "").split("|")]
        if not tipo or not venta or len(partes) < 4 or norm(partes[1]) != "santa fe":
            continue
        precios = venta.get("prices") or []
        precio_txt = f"{precios[0]['currency']} {precios[0]['price']}" if precios and p.get("webPrice") else ""
        unidad = "ha" if norm(p.get("surfaceMeasurement")) in ("ha", "hectareas") else "m²"
        resultados.append({
            "url": f"https://www.gaggiotti.com.ar/propiedad/compra-venta/{p['id']}/",
            "titulo": " ".join((p.get("publicationTitle") or "").split()),
            "ubicacion": f"{partes[3]}, {partes[2]}",  # "Rafaela, Castellanos" (sin el barrio)
            "precio_txt": precio_txt,
            "texto": f"{p.get('surface') or ''} {unidad}",
            "tipo": tipo,
        })
    return resultados, errores


def scrape_sitio(site):
    """Devuelve (avisos crudos, lista de errores). Un error en una URL no corta las demás."""
    if site.get("api") == "gaggiotti":
        return scrape_api_gaggiotti(site)
    resultados, errores = [], []
    for base in site["urls"]:
        url, pagina = base, 0
        while url and pagina < site.get("max_paginas", 3):
            try:
                html = descargar(url, site)
            except Exception as e:
                if not (pagina > 0 and "HTTP 404" in str(e)):  # 404 al paginar = no hay más páginas
                    errores.append(str(e))
                break
            soup = BeautifulSoup(html, "html.parser")
            items = soup.select(site["item"])
            for it in items:
                raw = leer_tarjeta(site, it, url)
                if not raw or any(r["url"] == raw["url"] for r in resultados):
                    continue
                if site.get("detalle"):
                    try:
                        time.sleep(1.5)
                        if not completar_con_detalle(site, raw):
                            continue
                    except Exception as e:
                        errores.append(f"detalle {e}")
                        continue
                resultados.append(raw)
            pagina += 1
            if site.get("pagina_url"):
                # Paginación por número de página (scroll infinito, etc.): se corta al llegar a una vacía.
                url = site["pagina_url"].format(url=base, n=pagina + 1) if items else None
            else:
                nxt = soup.select_one(site["siguiente"]) if site.get("siguiente") else None
                href = nxt and (nxt.get("href") or nxt.get("data-link-href"))
                url = urljoin(url, href) if href else None
            time.sleep(2)  # ser amable con el sitio
    return resultados, errores


def zona_de(ubicacion, modo=None):
    # El departamento se busca en la ubicación ("Calle 123, Localidad, Provincia") sin el primer
    # tramo cuando hay 3 o más, porque la calle puede llamarse como un pueblo (Sarmiento, López...).
    # Con modo "ultima" se usa solo el último tramo ("Barrio Sarmiento, Rafaela" -> "Rafaela").
    partes = [p for p in (ubicacion or "").split(",") if p.strip()]
    if modo == "ultima" and partes:
        # Tokko a veces pone "Localidad, Provincia" ("San Cristobal, Santa Fe"): ahí vale la localidad.
        # "Barrio X, Santa Fe" se deja como está (es un barrio de la ciudad de Santa Fe).
        if len(partes) > 1 and norm(partes[-1]) == "santa fe" and not norm(partes[-2]).startswith("barrio"):
            return partes[-2]
        return partes[-1]
    return ",".join(partes[1:]) if len(partes) >= 3 else (ubicacion or "")


def construir(site, raw):
    tipo = tipo_real(raw.get("tipo") or site["tipo"], raw["url"], raw["titulo"])
    if not tipo:
        return None
    zona = zona_de(raw["ubicacion"], site.get("zona"))
    depto, loc = departamento_de(zona if zona.strip() else raw["titulo"])
    if not depto:
        return None
    precio, moneda = parse_precio(raw["precio_txt"])
    return {
        "id": id_de(raw["url"]),
        "url": raw["url"], "fuente": site["nombre"], "tipo": tipo,
        "titulo": raw["titulo"], "ubicacion": raw["ubicacion"] or loc.title(),
        "departamento": depto, "precio": precio, "moneda": moneda,
        "hectareas": parse_hectareas(raw["titulo"] + " " + raw["texto"]) if tipo == "campo" else None,
    }


def tipo_de_texto(texto):
    """Tipo según la primera palabra clave que aparece ("Casa quinta" es casa, "Campo con casa" es campo)."""
    t = norm(texto)
    claves = [(r"\b(departamento|depto|monoambiente|local|oficina|cochera|lote|terreno)\b", None),
              (r"\b(casa|duplex|chalet)\b", "casa"),
              (r"\b(campo|chacra|establecimiento|hectareas?)\b|\b\d+\s*has?\b", "campo")]
    hallados = [(m.start(), tipo) for pat, tipo in claves if (m := re.search(pat, t))]
    return min(hallados, key=lambda x: x[0])[1] if hallados else None


def construir_alerta(raw):
    # El slug de la URL ("casa en venta en san carlos centro") es la fuente más confiable de tipo y localidad.
    tipo = tipo_de_texto(raw["slug"]) or tipo_de_texto(raw["titulo"])
    if not tipo:
        return None
    depto, loc = departamento_de(raw["slug"])
    if not depto:
        depto, loc = departamento_de(raw["titulo"] + " " + raw["texto"])
    if not depto:
        return None
    precio, moneda = parse_precio(raw["precio_txt"])
    return {
        "id": id_de(raw["url"]), "url": raw["url"], "fuente": raw["fuente"], "tipo": tipo, "via": "alerta",
        "titulo": raw["titulo"], "ubicacion": loc.title(), "departamento": depto,
        "precio": precio, "moneda": moneda,
        "hectareas": parse_hectareas(raw["titulo"] + " " + raw["slug"] + " " + raw["texto"]) if tipo == "campo" else None,
    }


def registrar(avisos, it, fecha):
    """Agrega un aviso nuevo o actualiza uno existente con lo visto en `fecha`."""
    a = avisos.get(it["id"])
    if a is None:
        it.update(primera_vez=fecha, ultima_vez=fecha, activo=True,
                  historial=[{"fecha": fecha, "precio": it["precio"], "moneda": it["moneda"]}])
        avisos[it["id"]] = it
        return
    a["primera_vez"] = min(a["primera_vez"], fecha)
    if fecha < a["ultima_vez"]:
        return  # dato más viejo que el guardado (por ejemplo, un email anterior): no pisar
    if it["precio"] and (it["precio"] != a.get("precio") or it["moneda"] != a.get("moneda")):
        if it["moneda"] == a.get("moneda") and a.get("precio"):
            a["precio_anterior"] = a["precio"]
        else:
            a.pop("precio_anterior", None)
        a["historial"].append({"fecha": fecha, "precio": it["precio"], "moneda": it["moneda"]})
    for k in ("titulo", "ubicacion", "departamento", "precio", "moneda", "hectareas"):
        if it[k] is not None:
            a[k] = it[k]
    a["ultima_vez"], a["activo"] = fecha, True


def procesar_alertas(avisos, resumen, test):
    cfg = CFG.get("alertas", {})
    if not cfg.get("activo", True):
        return
    if not os.environ.get("GMAIL_USER"):
        print("[Alertas por email] sin configurar (faltan GMAIL_USER / GMAIL_APP_PASSWORD): se omiten")
        return
    import alertas
    try:
        raws, n_emails = alertas.leer_gmail(cfg)
    except Exception as e:
        print(f"[ERROR] Alertas por email: {e}")
        resumen.append({"fuente": "Alertas por email", "tipo": "todos", "leidos": 0, "en_zona": 0, "errores": 1})
        return
    items = [(x, r["fecha"]) for r in raws if (x := construir_alerta(r))]
    print(f"[Alertas por email] {n_emails} emails, {len(raws)} avisos leídos, {len(items)} en la zona")
    resumen.append({"fuente": "Alertas por email", "tipo": "todos", "leidos": len(raws),
                    "en_zona": len(items), "errores": 0})
    if test:
        for x, f in items:
            print(f"  {f} {x['fuente']} {x['tipo']} {x['departamento']}: {x['titulo'][:60]} ({x['precio']} {x['moneda']})")
        return
    for x, fecha in sorted(items, key=lambda p: p[1]):  # de más viejo a más nuevo, para armar bien el historial
        registrar(avisos, x, fecha)


def sanear_previos(avisos_previos):
    """Aplica las reglas actuales a los datos guardados: corrige tipos, precios inválidos y duplicados."""
    out = {}
    for a in avisos_previos:
        tipo = tipo_real(a["tipo"], a["url"], a["titulo"])
        if not tipo:
            continue
        a["tipo"] = tipo
        if tipo != "campo":
            a["hectareas"] = None
        if a.get("precio") is not None and a["precio"] < PRECIO_MINIMO:
            a["precio"] = None
        a["historial"] = [h for h in a.get("historial", []) if h.get("precio") is None or h["precio"] >= PRECIO_MINIMO]
        if a.get("precio_anterior") is not None and a["precio_anterior"] < PRECIO_MINIMO:
            a.pop("precio_anterior")
        a["id"] = id_de(a["url"])
        b = out.get(a["id"])
        if b:  # duplicado: conservar el más antiguo y la última fecha vista
            b["primera_vez"] = min(b["primera_vez"], a["primera_vez"])
            b["ultima_vez"] = max(b["ultima_vez"], a["ultima_vez"])
            b["activo"] = b["activo"] or a["activo"]
        else:
            out[a["id"]] = a
    return out


def main():
    test = "--test" in sys.argv
    hoy = date.today().isoformat()
    previo = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"avisos": []}
    avisos = sanear_previos(previo["avisos"])
    ok_fuentes, fuentes_con_fallas = set(), set()
    resumen = []

    for site in CFG["sitios"]:
        if not site.get("activo", True):
            continue
        raws, errores = scrape_sitio(site)
        for e in errores:
            print(f"[ERROR] {site['nombre']} ({site['tipo']}): {e}")
        vistos = {}
        for x in (construir(site, r) for r in raws):
            if x:
                vistos.setdefault(x["id"], x)
        items = list(vistos.values())
        print(f"[{site['nombre']} / {site['tipo']}] {len(raws)} avisos leídos, {len(items)} en la zona"
              + (f", {len(errores)} URL con error" if errores else ""))
        resumen.append({"fuente": site["nombre"], "tipo": site["tipo"], "leidos": len(raws),
                        "en_zona": len(items), "errores": len(errores)})
        # 0 avisos = probable cambio de HTML o bloqueo, salvo en búsquedas que pueden estar vacías
        # (p. ej. una inmobiliaria sin campos publicados; sus selectores se validan con la de casas).
        if errores or (not raws and not site.get("puede_estar_vacio")):
            fuentes_con_fallas.add(site["nombre"])
        else:
            ok_fuentes.add(site["nombre"])
        if test or not raws:
            continue
        for it in items:
            registrar(avisos, it, hoy)

    procesar_alertas(avisos, resumen, test)

    if test:
        return
    limite = CFG.get("dias_para_dar_de_baja", 3)
    limite_alertas = CFG.get("alertas", {}).get("dias_activo", 45)
    for a in avisos.values():
        if a.get("via") == "alerta":
            # Las alertas solo avisan lo nuevo o lo que cambió: un aviso se considera vigente
            # durante `dias_activo` desde el último email que lo mencionó.
            a["activo"] = (date.fromisoformat(hoy) - date.fromisoformat(a["ultima_vez"])).days < limite_alertas
        # Solo se da de baja si todas las búsquedas de esa fuente se leyeron bien (un aviso reclasificado
        # de campo a casa puede venir de cualquiera de ellas).
        elif a["fuente"] in ok_fuentes - fuentes_con_fallas:
            dias = (date.fromisoformat(hoy) - date.fromisoformat(a["ultima_vez"])).days
            a["activo"] = dias < limite

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"actualizado": datetime.now().isoformat(timespec="minutes"),
         "fuentes": resumen,
         "avisos": sorted(avisos.values(), key=lambda a: (a["primera_vez"], a["id"]), reverse=True)},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Guardado: {len(avisos)} avisos en total ({sum(a['activo'] for a in avisos.values())} activos).")


if __name__ == "__main__":
    main()
