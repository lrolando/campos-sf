# Campos y casas · centro-oeste de Santa Fe

Un workflow de GitHub Actions corre todos los días, busca avisos, actualiza `docs/data/listings.json` y el sitio (`docs/index.html`) los muestra con enlace a la publicación original.

## Puesta en marcha
1. Subí esta carpeta a un repo nuevo en GitHub.
2. **Settings → Pages**: Source = *Deploy from a branch*, branch `main`, carpeta `/docs`.
3. **Settings → Actions → General → Workflow permissions**: *Read and write permissions*.
4. **Actions → Actualización diaria → Run workflow** para la primera corrida.

## Ajustar los sitios (importante)
Los selectores CSS de `config.json` son un punto de partida y los sitios cambian su HTML. Probalos en tu PC:

    pip install -r requirements.txt
    python scraper/main.py --test

Si un sitio muestra 0 avisos, abrí la página en el navegador, inspeccioná una tarjeta y corregí `item`, `link`, `titulo`, `precio` y `ubicacion`. Para sumar una inmobiliaria local, copiá un bloque de `sitios` con su URL y sus selectores.
Si un sitio devuelve 0 avisos, el script no da de baja nada de ese sitio.

Opciones extra por sitio (todas opcionales):
- `titulo`, `precio`, `ubicacion` aceptan `"selector@atributo"` (lee un atributo) o una lista de selectores (une sus textos).
- `quitar`: selectores que se borran de la tarjeta antes de leerla (p. ej. un código de aviso dentro del precio).
- `solo_si` / `excluir_si`: regex sobre el texto de la tarjeta / el título (p. ej. `"en venta"`, `"vendido"`).
- `ubicacion_regex`, `ubicacion_fija`, `zona: "ultima"`: cómo sacar la localidad.
- `tipo` = `"todos"` + `tipo_regex` (sobre el título) o `detalle.tipo_regex`: el tipo se saca de cada aviso.
- `detalle`: entra a la página de cada aviso y saca `precio_regex` / `tipo_regex` de su texto.
- `pagina_url`: paginación por número (`{url}`, `{n}`); se corta en la primera página vacía o 404.
- `puede_estar_vacio`: una búsqueda con 0 avisos no cuenta como falla (inmobiliaria sin campos publicados).
- `conservar_query`: no quita el `?id=...` de la URL del aviso.
- `api: "gaggiotti"`: Gaggiotti arma su web con JavaScript; se lee la API de búsqueda que usa su propio sitio
  (solo lectura). `tipos_api` mapea los códigos de tipo de esa API a campo/casa.

Plantillas listas para copiar en `config.json`: **Tokko Broker** (bloques de Avantix / Propiedades Rafaela),
**Pixel Inmobiliario** (Walker / Del Lago / Rubiolo: cambia `user_id` y `type`) y **WordPress Houzez** (Meyer).

## Qué hace el script con los datos
- Corrige el tipo: si una búsqueda de *campos* trae casas, se guardan como casa; los departamentos se descartan.
- Precios menores a 1.000 (placeholders como "USD 1") se muestran como "Consultar".
- Un mismo aviso de Mercado Libre publicado en varios subdominios se guarda una sola vez (por su código MLA).
- Si alguna URL de una fuente falla, esa fuente no da de baja avisos en esa corrida.
- `listings.json` incluye `fuentes` con el resultado de la última corrida; el pie del sitio lo muestra.

## El sitio
Filtros por tipo, departamento, fuente, precio máximo (USD) y hectáreas mínimas; orden por USD/ha; historial de precios. Los filtros quedan en la URL, así que se pueden compartir.

No subas `scraper/_waf.txt` (está en `.gitignore`): contiene un token de sesión.

## Zonaprop y Argenprop: por alertas de email
Esos sitios bloquean el acceso automático, así que no se consultan (`"activo": false` en `config.json`).
Sus avisos se toman de los emails de alerta que te mandan, leídos desde Gmail en modo solo lectura.

1. En Zonaprop y Argenprop, armá las búsquedas (campos y casas en venta en cada zona) y activá
   **"Recibir alertas por email"** con frecuencia diaria.
2. En Gmail, creá un filtro: *De:* `zonaprop.com.ar OR argenprop.com` → **Aplicar etiqueta** `alertas-inmuebles`.
   (El nombre de la etiqueta se puede cambiar en `config.json` → `alertas.etiqueta`.)
3. En tu cuenta de Google activá la verificación en 2 pasos y creá una **contraseña de aplicación**
   (myaccount.google.com/apppasswords). Son 16 letras.
4. En GitHub: **Settings → Secrets and variables → Actions → New repository secret**:
   `GMAIL_USER` (tu dirección) y `GMAIL_APP_PASSWORD` (la contraseña de aplicación).

Probalo en tu PC con un email de alerta descargado desde Gmail (⋮ → *Descargar mensaje*):

    python scraper/alertas.py --eml alerta.eml

Si muestra 0 avisos, el formato del email es distinto al esperado: mandame ese archivo y ajusto el lector.

- Se leen los emails de los últimos `dias_hacia_atras` días; no se marcan como leídos ni se modifican.
- Las alertas solo avisan lo nuevo, así que un aviso queda activo `dias_activo` días (45) desde el último email que lo nombró.
- La contraseña de aplicación solo vive en los Secrets de GitHub. No la pongas en ningún archivo del repo.
