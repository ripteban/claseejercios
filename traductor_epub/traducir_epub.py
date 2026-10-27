#!/usr/bin/env python3
"""
traducir_epub.py — Traduce un EPUB (2 o 3) al español con Google Translate (gratis,
vía deep-translator, sin clave de API), conservando intacta la estructura: archivos,
imágenes, CSS, índice (NCX y nav), enlaces, anclas, notas al pie y metadatos.
Solo cambia el texto.

Uso rápido:
    pip install deep-translator lxml
    python traducir_epub.py MiLibro.epub --estimar
    python traducir_epub.py MiLibro.epub --glosario glosario.json
    python traducir_epub.py MiLibro.epub --simulado      # prueba sin conexión

Úsalo solo con libros de tu autoría, de dominio público o con licencia de traducción.
"""

import argparse
import copy
import html.entities
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from lxml import etree

NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

# Elementos que delimitan bloques de texto: se traduce cada bloque "hoja" completo.
BLOQUES = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "td", "th",
    "caption", "figcaption", "blockquote", "div", "section", "article", "aside",
    "header", "footer", "nav", "main", "ol", "ul", "dl", "table", "thead",
    "tbody", "tfoot", "tr", "figure", "body", "pre", "hr", "address",
}
# Contenido que nunca se traduce (se conserva byte a byte).
INTOCABLES = {"script", "style", "code", "pre", "kbd", "samp", "var", "math", "svg"}
VACIOS = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col",
          "embed", "param", "source", "track", "wbr"}
ATRIBUTOS_TRADUCIBLES = ("alt", "title", "aria-label")
PARECE_ARCHIVO = re.compile(r"^[\w\-. ]+\.(jpe?g|png|gif|svg|webp|bmp|tiff?)$", re.I)
LETRA = re.compile(r"[^\W\d_]")
ENTIDAD = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);?")
XML_BASICAS = {"amp", "lt", "gt", "quot", "apos"}

# ---------------------------------------------------------------- utilidades XML

def local(el):
    return etree.QName(el).localname.lower() if isinstance(el.tag, str) else None


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tiene_letras(s):
    return bool(s and LETRA.search(s))


def _entidades_a_numericas(texto):
    """Convierte entidades HTML con nombre (&nbsp;, &mdash;...) en numéricas válidas en XML
    y escapa los & sueltos."""
    def rep(m):
        nombre = m.group(1)
        if not m.group(0).endswith(";"):
            return "&amp;" + m.group(0)[1:]
        if nombre.startswith("#") or nombre in XML_BASICAS:
            return m.group(0)
        cp = html.entities.name2codepoint.get(nombre)
        return f"&#{cp};" if cp else "&amp;" + nombre + ";"
    texto = re.sub(r"&(?![#A-Za-z])", "&amp;", texto)
    return ENTIDAD.sub(rep, texto)


def parsear_fragmento(s):
    return etree.fromstring("<r>" + _entidades_a_numericas(s) + "</r>")


def estructura(s):
    """Firma de los marcadores <tN> de un segmento: (marcador, padre, vacío)."""
    raiz = parsear_fragmento(s)
    firma = []
    for e in raiz.iterdescendants():
        if not isinstance(e.tag, str) or not re.fullmatch(r"t\d+", e.tag):
            raise ValueError(f"etiqueta inesperada: {e.tag!r}")
        vacio = len(e) == 0 and not (e.text or "").strip()
        firma.append((e.tag, e.getparent().tag, vacio))
    return sorted(firma)


def parsear_archivo(ruta):
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True,
                             remove_blank_text=False, strip_cdata=False, huge_tree=True)
    datos = Path(ruta).read_bytes()
    try:
        return etree.ElementTree(etree.fromstring(datos, parser))
    except etree.XMLSyntaxError:
        # Entidades HTML no declaradas: se pasan a numéricas y se reintenta.
        texto = datos.decode("utf-8", errors="replace")
        texto = re.sub(r"&([A-Za-z][A-Za-z0-9]*);",
                       lambda m: m.group(0) if m.group(1) in XML_BASICAS else
                       (f"&#{html.entities.name2codepoint[m.group(1)]};"
                        if m.group(1) in html.entities.name2codepoint else m.group(0)),
                       texto)
        return etree.ElementTree(etree.fromstring(texto.encode("utf-8"), parser))


def escribir_archivo(arbol, ruta, es_xhtml):
    if es_xhtml:
        # Evita <a/> o <div/> autocerrados, que algunos lectores interpretan como HTML.
        for el in arbol.getroot().iter():
            if isinstance(el.tag, str) and len(el) == 0 and el.text is None \
                    and local(el) not in VACIOS:
                el.text = ""
    datos = etree.tostring(arbol, encoding="utf-8", xml_declaration=True,
                           doctype=arbol.docinfo.doctype or None)
    Path(ruta).write_bytes(datos)


# ---------------------------------------------------------------- unidades de traducción

class UnidadElemento:
    """Un bloque completo (p. ej. un párrafo). El formato interno se sustituye por
    marcadores <tN> que el modelo puede reubicar; al aplicar se reutilizan los
    elementos originales con todos sus atributos."""

    def __init__(self, el):
        self.el = el
        self.slots, self.opacos = {}, set()
        completo = self._codificar(el)
        nucleo = completo.strip()
        inicio = completo.find(nucleo) if nucleo else 0
        self.ini, self.fin = completo[:inicio], completo[inicio + len(nucleo):]
        self.fuente = nucleo

    def _codificar(self, nodo):
        partes = [esc(nodo.text or "")]
        for hijo in nodo:
            n = len(self.slots)
            self.slots[n] = hijo
            opaco = (not isinstance(hijo.tag, str) or local(hijo) in INTOCABLES
                     or (len(hijo) == 0 and not (hijo.text or "").strip()))
            if opaco:
                self.opacos.add(n)
                partes.append(f"<t{n}/>")
            else:
                partes.append(f"<t{n}>{self._codificar(hijo)}</t{n}>")
            partes.append(esc(hijo.tail or ""))
        return "".join(partes)

    def aplicar(self, traduccion):
        frag = parsear_fragmento(self.ini + traduccion + self.fin)

        def construir(fnodo, destino):
            for c in list(destino):
                destino.remove(c)
            destino.text = fnodo.text
            for f in fnodo:
                n = int(f.tag[1:])
                original = self.slots[n]
                if n not in self.opacos:
                    construir(f, original)
                original.tail = f.tail
                destino.append(original)

        construir(frag, self.el)


class UnidadTexto:
    """Un nodo de texto suelto, un tail o un atributo (alt, title...)."""

    def __init__(self, el, campo):
        self.el, self.campo = el, campo
        valor = self._leer()
        nucleo = valor.strip()
        inicio = valor.find(nucleo) if nucleo else 0
        self.ini, self.fin = valor[:inicio], valor[inicio + len(nucleo):]
        self.fuente = esc(nucleo)

    def _leer(self):
        if self.campo == "text":
            return self.el.text or ""
        if self.campo == "tail":
            return self.el.tail or ""
        return self.el.get(self.campo[1:]) or ""

    def aplicar(self, traduccion):
        frag = parsear_fragmento(traduccion)
        if len(frag):
            raise ValueError("un texto plano no puede contener etiquetas")
        valor = self.ini + (frag.text or "") + self.fin
        if self.campo == "text":
            self.el.text = valor
        elif self.campo == "tail":
            self.el.tail = valor
        else:
            self.el.set(self.campo[1:], valor)


def _contiene_bloque(el):
    return any(local(d) in BLOQUES for d in el.iterdescendants())


def _texto_visible(el):
    return "".join(t for t in el.itertext())


def _descender(el):
    """<li><a>Capítulo 1</a></li> -> se traduce el <a> (así coincide con el NCX)."""
    while True:
        hijos = list(el)
        if (len(hijos) == 1 and isinstance(hijos[0].tag, str)
                and local(hijos[0]) not in INTOCABLES
                and not (el.text or "").strip() and not (hijos[0].tail or "").strip()):
            el = hijos[0]
        else:
            return el


def _recorrer(el, unidades):
    if local(el) in INTOCABLES:
        return
    if not _contiene_bloque(el):
        if tiene_letras(_texto_visible(el)):
            unidades.append(UnidadElemento(_descender(el)))
        return
    if tiene_letras(el.text):
        unidades.append(UnidadTexto(el, "text"))
    for hijo in el:
        if isinstance(hijo.tag, str):
            _recorrer(hijo, unidades)
        if tiene_letras(hijo.tail):
            unidades.append(UnidadTexto(hijo, "tail"))


def unidades_xhtml(arbol):
    raiz = arbol.getroot()
    unidades = []
    for hijo in raiz:
        if local(hijo) == "head":
            for t in hijo:
                if local(t) == "title" and tiene_letras(_texto_visible(t)):
                    unidades.append(UnidadElemento(t))
        elif local(hijo) == "body":
            _recorrer(hijo, unidades)
    for el in raiz.iter():
        if not isinstance(el.tag, str):
            continue
        for a in ATRIBUTOS_TRADUCIBLES:
            v = el.get(a)
            if v and tiene_letras(v) and not PARECE_ARCHIVO.match(v.strip()):
                unidades.append(UnidadTexto(el, "@" + a))
    return unidades


def unidades_ncx(arbol):
    unidades = []
    for el in arbol.getroot().iter():
        if local(el) == "text" and tiene_letras(el.text):
            padre = el.getparent()
            if padre is not None and local(padre) == "docauthor":
                continue
            unidades.append(UnidadElemento(el))
    return unidades


def unidades_opf(arbol):
    unidades = []
    for el in arbol.getroot().iter(f"{{{NS_DC}}}title", f"{{{NS_DC}}}description"):
        if tiene_letras(el.text):
            unidades.append(UnidadElemento(el))
    return unidades


def explotar(unidad):
    """Plan B si un bloque no pasa la validación: traducir sus nodos de texto uno a uno."""
    res = []

    def caminar(nodo):
        if tiene_letras(nodo.text):
            res.append(UnidadTexto(nodo, "text"))
        for hijo in nodo:
            if isinstance(hijo.tag, str) and local(hijo) not in INTOCABLES:
                caminar(hijo)
            if tiene_letras(hijo.tail):
                res.append(UnidadTexto(hijo, "tail"))

    caminar(unidad.el)
    return res


# ---------------------------------------------------------------- traductor

# Google a veces devuelve los marcadores con espacios: "< t0 >", "</ t0>", "<t1 />".
MARCADOR = re.compile(r"<\s*(/?)\s*[tT]\s*(\d+)\s*(/?)\s*>")
CANONICO = re.compile(r"</?t\d+/?>")
# Los términos del glosario viajan como ZXQ0, ZXQ1... para que Google no los toque.
CLAVE_GLOSARIO = re.compile(r"(?<![A-Za-z])Z\s*X\s*Q\s*(\d+)", re.I)
ESPACIOS = re.compile(r"[ \t\r\n]+")  # no incluye el espacio duro (U+00A0)
ANTES_DE_FRASE = re.compile(r"(?:\s|</?t\d+/?>|[“\"«(—])*$")


class TraductorGoogle:
    """Google Translate gratuito, vía deep-translator: sin clave ni costo. No acepta
    instrucciones, así que el glosario se aplica protegiendo los términos."""

    LIMITE = 4900  # Google rechaza textos de 5000 caracteres o más

    def __init__(self, args, origen, glosario):
        from deep_translator import GoogleTranslator  # importación diferida: --simulado funciona sin la librería
        self.clase = GoogleTranslator
        self.origen, self.destino = origen, args.idioma.split("-")[0].lower()
        self.clase(source=self.origen, target=self.destino)  # valida los idiomas antes de empezar
        self.lote = max(500, min(args.lote_caracteres, self.LIMITE))
        self.pausa = args.pausa
        self.local = threading.local()  # cada hilo usa su propia instancia (no es thread-safe)
        self.uso = Counter()
        self.lock = threading.Lock()
        self.terminos = sorted(glosario.get("terminos", {}).items(), key=lambda kv: -len(kv[0]))
        self.indice = {k: n for n, (k, _) in enumerate(self.terminos)}
        self.patron_glosario = re.compile(
            r"(?<!\w)(" + "|".join(re.escape(k) for k, _ in self.terminos) + r")(?!\w)"
        ) if self.terminos else None

    # --- preparación y restauración del texto

    def _preparar(self, seg):
        """Segmento con marcadores <tN> -> texto plano de una línea para Google."""
        texto = html.unescape(ESPACIOS.sub(" ", seg))  # &amp; &lt; &gt; -> & < >
        if self.patron_glosario:
            texto = self.patron_glosario.sub(lambda m: f"ZXQ{self.indice[m.group(1)]}", texto)
        return texto

    def _termino(self, m):
        n = int(m.group(1))
        if n >= len(self.terminos):
            return m.group(0)
        valor = self.terminos[n][1]
        previo = ANTES_DE_FRASE.sub("", m.string[:m.start()])
        if not previo or previo[-1] in ".!?…¿¡":
            valor = valor[:1].upper() + valor[1:]
        return valor

    def _restaurar(self, texto):
        """Respuesta de Google -> segmento con marcadores canónicos y & < > escapados."""
        texto = MARCADOR.sub(lambda m: f"<{m.group(1)}t{m.group(2)}{m.group(3)}>", texto.strip())
        if self.terminos:
            texto = CLAVE_GLOSARIO.sub(self._termino, texto)
        partes, pos = [], 0
        for m in CANONICO.finditer(texto):
            partes += [esc(texto[pos:m.start()]), m.group(0)]
            pos = m.end()
        texto = "".join(partes) + esc(texto[pos:])
        # Espacios que Google suele dejar junto a los marcadores.
        texto = re.sub(r"(<t\d+>) +", r" \1", texto)
        texto = re.sub(r" +(</t\d+>)", r"\1 ", texto)
        texto = re.sub(r"([¿¡]) +", r"\1", texto)
        texto = re.sub(r" +([,.;:!?…)»”])", r"\1", texto)
        return re.sub(r" {2,}", " ", texto).strip()

    # --- peticiones

    def _google(self):
        g = getattr(self.local, "g", None)
        if g is None:
            g = self.local.g = self.clase(source=self.origen, target=self.destino)
        return g

    def _pedir(self, texto, intentos=6):
        for intento in range(intentos):
            try:
                if self.pausa:
                    time.sleep(self.pausa)
                r = self._google().translate(texto)
                with self.lock:
                    self.uso["peticiones"] += 1
                    self.uso["caracteres"] += len(texto)
                return texto if r is None else r
            except Exception as e:
                if intento == intentos - 1:
                    raise
                espera = min(60, 2 ** (intento + 1))
                print(f"  ! Google falló ({type(e).__name__}); reintento en {espera}s.",
                      file=sys.stderr)
                time.sleep(espera)

    def _trozos(self, texto):
        """Parte un párrafo demasiado largo en frases que quepan en una petición."""
        if len(texto) <= self.lote:
            return [texto]
        trozos, actual = [], ""
        for f in re.split(r"(?<=[.!?…])\s+", texto):
            while len(f) > self.lote:  # frase gigantesca: corte en un espacio
                corte = f.rfind(" ", 0, self.lote)
                corte = corte if corte > 0 else self.lote
                if actual:
                    trozos.append(actual)
                    actual = ""
                trozos.append(f[:corte])
                f = f[corte:].lstrip()
            if actual and len(actual) + 1 + len(f) > self.lote:
                trozos.append(actual)
                actual = f
            else:
                actual = f"{actual} {f}" if actual else f
        if actual:
            trozos.append(actual)
        return trozos

    def _traducir_varios(self, textos):
        """Varios textos en una sola petición, uno por línea. Si Google no devuelve la
        misma cantidad de líneas, divide el grupo a la mitad."""
        if len(textos) == 1:
            return [" ".join(self._pedir(t) for t in self._trozos(textos[0]))]
        lineas = [l for l in self._pedir("\n".join(textos)).split("\n") if l.strip()]
        if len(lineas) == len(textos):
            return lineas
        mitad = len(textos) // 2
        return self._traducir_varios(textos[:mitad]) + self._traducir_varios(textos[mitad:])

    def traducir(self, items):
        planos = [(i, self._preparar(t)) for i, t in items]
        grupos, actual, tam = [], [], 0
        for i, t in planos:
            if actual and tam + len(t) + 1 > self.lote:
                grupos.append(actual)
                actual, tam = [], 0
            actual.append((i, t))
            tam += len(t) + 1
        if actual:
            grupos.append(actual)
        salida = {}
        for g in grupos:
            for (i, _), r in zip(g, self._traducir_varios([t for _, t in g])):
                salida[i] = self._restaurar(r)
        return salida


class TraductorSimulado:
    """Para probar la estructura sin conexión: antepone «[ES] » a cada segmento."""
    uso = Counter()

    def traducir(self, items):
        return {i: "[ES] " + t for i, t in items}


def traducir_lote(traductor, items, firmas, intentos=3):
    """Traduce y valida un lote. Devuelve (traducciones válidas, ids fallidos)."""
    pendientes = dict(items)
    hechas = {}
    for intento in range(intentos):
        if not pendientes:
            break
        try:
            salida = traductor.traducir(list(pendientes.items()))
        except Exception as e:
            print(f"  ! No se pudo traducir un lote (intento {intento + 1}): "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            continue
        for i, t in salida.items():
            if i not in pendientes:
                continue
            try:
                if estructura(t) == firmas[i]:
                    hechas[i] = t
                    del pendientes[i]
            except (etree.XMLSyntaxError, ValueError):
                pass
    return hechas, list(pendientes)


# ---------------------------------------------------------------- EPUB

def extraer(epub, destino):
    with zipfile.ZipFile(epub) as z:
        raiz = Path(destino).resolve()
        for nombre in z.namelist():
            ruta = (raiz / nombre).resolve()
            if raiz not in ruta.parents and ruta != raiz:
                raise ValueError(f"Ruta insegura dentro del EPUB: {nombre}")
        z.extractall(destino)
        return z.namelist()


def empaquetar(origen, nombres, salida):
    with zipfile.ZipFile(salida, "w") as z:
        if "mimetype" in nombres:
            z.write(Path(origen, "mimetype"), "mimetype", compress_type=zipfile.ZIP_STORED)
        for nombre in nombres:
            if nombre == "mimetype" or nombre.endswith("/"):
                continue
            z.write(Path(origen, nombre), nombre, compress_type=zipfile.ZIP_DEFLATED)


def leer_paquete(carpeta):
    contenedor = etree.parse(str(Path(carpeta, "META-INF", "container.xml")))
    rootfile = contenedor.getroot().find(f".//{{{NS_CONTAINER}}}rootfile")
    opf_rel = rootfile.get("full-path")
    opf = Path(carpeta, opf_rel)
    base = PurePosixPath(opf_rel).parent
    arbol = parsear_archivo(opf)
    manifiesto = {}
    for item in arbol.getroot().iter(f"{{{NS_OPF}}}item"):
        href = unquote(item.get("href", "").split("#")[0])
        manifiesto[item.get("id")] = (str(base / href), item.get("media-type", ""))
    lomo = [manifiesto[ir.get("idref")][0]
            for ir in arbol.getroot().iter(f"{{{NS_OPF}}}itemref") if ir.get("idref") in manifiesto]
    return opf_rel, arbol, manifiesto, lomo


def ajustar_idioma(arbol, origen, destino):
    raiz = arbol.getroot()
    for el in raiz.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in (XML_LANG, "lang"):
            v = el.get(attr)
            if v and (v.lower().startswith(origen.lower()) or el is raiz):
                el.set(attr, destino)
    if raiz.get(XML_LANG) is None and raiz.get("lang") is None:
        raiz.set(XML_LANG, destino)


def ajustar_opf(arbol, origen, destino, traductor_nombre):
    raiz = arbol.getroot()
    for el in raiz.iter(f"{{{NS_DC}}}language"):
        if (el.text or "").strip().lower().startswith(origen.lower()):
            el.text = destino
    if raiz.get(XML_LANG):
        raiz.set(XML_LANG, destino)
    if traductor_nombre:
        meta = raiz.find(f"{{{NS_OPF}}}metadata")
        c = etree.SubElement(meta, f"{{{NS_DC}}}contributor")
        c.text = traductor_nombre
        if raiz.get("version", "2.0").startswith("3"):
            c.set("id", "traductor")
            m = etree.SubElement(meta, f"{{{NS_OPF}}}meta",
                                 refines="#traductor", property="role", scheme="marc:relators")
            m.text = "trl"
        else:
            c.set(f"{{{NS_OPF}}}role", "trl")


def firma_estructural(arbol):
    """Lo que no debe cambiar: etiquetas, ids, enlaces e imágenes."""
    c = Counter()
    for el in arbol.getroot().iter():
        if isinstance(el.tag, str):
            c[(local(el), el.get("id"), el.get("href"), el.get("src"),
               el.get("{http://www.w3.org/1999/xlink}href"), el.get("class"))] += 1
    return c


# ---------------------------------------------------------------- programa principal

def agrupar(fuentes, max_chars):
    lotes, actual, tam = [], [], 0
    for s in fuentes:
        if actual and tam + len(s) > max_chars:
            lotes.append(actual)
            actual, tam = [], 0
        actual.append(s)
        tam += len(s)
    if actual:
        lotes.append(actual)
    return lotes


def main():
    ap = argparse.ArgumentParser(description="Traduce un EPUB al español con Google Translate "
                                             "(gratis) conservando formato, imágenes e índice.")
    ap.add_argument("entrada", help="EPUB original")
    ap.add_argument("-o", "--salida", help="EPUB traducido (por defecto: <nombre>_es.epub)")
    ap.add_argument("--idioma", default="es-419",
                    help="código del idioma destino para los metadatos (es-419); "
                         "a Google se le pide la parte principal (es)")
    ap.add_argument("--origen", help="idioma original para Google (por defecto el del OPF; "
                                     "usa 'auto' para que lo detecte)")
    ap.add_argument("--glosario", help="JSON con {\"terminos\": {...}}")
    ap.add_argument("--hilos", type=int, default=2, help="peticiones simultáneas")
    ap.add_argument("--lote-caracteres", type=int, default=4000,
                    help="caracteres por petición (máximo 4900)")
    ap.add_argument("--pausa", type=float, default=0.5,
                    help="segundos de espera antes de cada petición")
    ap.add_argument("--cache", help="archivo de progreso (por defecto junto a la salida)")
    ap.add_argument("--traductor", help="nombre del traductor para los metadatos")
    ap.add_argument("--simulado", action="store_true", help="no se conecta a Google")
    ap.add_argument("--estimar", action="store_true", help="solo calcula el volumen")
    ap.add_argument("--epubcheck", help="ruta a epubcheck.jar para validar el resultado")
    args = ap.parse_args()
    for flujo in (sys.stdout, sys.stderr):
        flujo.reconfigure(errors="replace")  # consolas de Windows sin UTF-8

    entrada = Path(args.entrada).resolve()
    salida = Path(args.salida or entrada.with_name(entrada.stem + "_es.epub")).resolve()
    ruta_cache = Path(args.cache or salida.with_suffix(".progreso.json"))
    glosario = json.loads(Path(args.glosario).read_text("utf-8")) if args.glosario else {}

    trabajo = Path(tempfile.mkdtemp(prefix="epub_trad_"))
    try:
        original = trabajo / "original"
        nuevo = trabajo / "nuevo"
        nombres = extraer(entrada, original)
        shutil.copytree(original, nuevo)

        opf_rel, opf_arbol, manifiesto, lomo = leer_paquete(nuevo)
        idioma_origen = "en"
        for el in opf_arbol.getroot().iter(f"{{{NS_DC}}}language"):
            idioma_origen = (el.text or "en").strip().split("-")[0] or "en"
            break

        xhtml = [r for r, mt in manifiesto.values() if mt in ("application/xhtml+xml", "text/html")]
        xhtml.sort(key=lambda r: lomo.index(r) if r in lomo else len(lomo))
        ncx = [r for r, mt in manifiesto.values() if mt == "application/x-dtbncx+xml"]

        # 1. Extraer unidades traducibles en orden de lectura.
        documentos = []  # (ruta, árbol, unidades, tipo)
        for rel in xhtml:
            arbol = parsear_archivo(nuevo / rel)
            documentos.append((rel, arbol, unidades_xhtml(arbol), "xhtml"))
        for rel in ncx:
            arbol = parsear_archivo(nuevo / rel)
            documentos.append((rel, arbol, unidades_ncx(arbol), "ncx"))
        documentos.append((opf_rel, opf_arbol, unidades_opf(opf_arbol), "opf"))

        orden, vistos = [], set()
        for _, _, unidades, _ in documentos:
            for u in unidades:
                if u.fuente and u.fuente not in vistos:
                    vistos.add(u.fuente)
                    orden.append(u.fuente)

        cache = json.loads(ruta_cache.read_text("utf-8")) if ruta_cache.exists() else {}
        pendientes = [s for s in orden if s not in cache]
        total_chars = sum(len(s) for s in pendientes)
        print(f"Archivos de texto: {len(xhtml)} XHTML, {len(ncx)} NCX, 1 OPF")
        print(f"Segmentos únicos: {len(orden)} (pendientes {len(pendientes)}, "
              f"ya traducidos {len(orden) - len(pendientes)})")
        print(f"Caracteres pendientes: {total_chars:,}")

        lotes = agrupar(pendientes, min(args.lote_caracteres, TraductorGoogle.LIMITE))

        if args.estimar:
            segundos = len(lotes) * (args.pausa + 1.5) / max(1, args.hilos)
            print(f"Peticiones a Google: ~{len(lotes)}  |  tiempo aproximado: "
                  f"{segundos / 60:,.0f} min  |  costo: gratis")
            return

        if glosario.get("notas"):
            print("Aviso: Google Translate no acepta notas de estilo; se usan solo los "
                  "«terminos» del glosario.")
        origen_google = args.origen or idioma_origen
        traductor = (TraductorSimulado() if args.simulado
                     else TraductorGoogle(args, origen_google, glosario))
        lock = threading.Lock()
        fallidos_globales = []

        def guardar_cache():
            tmp = ruta_cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=0), "utf-8")
            os.replace(tmp, ruta_cache)

        def ejecutar_ronda(fuentes, etiqueta):
            firmas = {}
            for s in fuentes:
                try:
                    firmas[s] = estructura(s)
                except (etree.XMLSyntaxError, ValueError):
                    firmas[s] = None
            fuentes = [s for s in fuentes if firmas[s] is not None]
            lotes = agrupar(fuentes, min(args.lote_caracteres, TraductorGoogle.LIMITE))
            hechos, inicio = 0, time.time()
            fallidos = []

            def tarea(lote):
                items = list(enumerate(lote))
                f = {i: firmas[s] for i, s in items}
                ok, mal = traducir_lote(traductor, items, f)
                return lote, ok, mal

            with ThreadPoolExecutor(max_workers=max(1, args.hilos)) as ex:
                futuros = [ex.submit(tarea, l) for l in lotes]
                for fut in as_completed(futuros):
                    lote, ok, mal = fut.result()
                    with lock:
                        for i, t in ok.items():
                            cache[lote[i]] = t
                        fallidos.extend(lote[i] for i in mal)
                        guardar_cache()
                        hechos += 1
                        trans = time.time() - inicio
                        print(f"  {etiqueta}: lote {hechos}/{len(lotes)} "
                              f"({trans / hechos * (len(lotes) - hechos):,.0f}s restantes aprox.)")
            return fallidos

        print(f"Traduciendo con {'modo simulado' if args.simulado else 'Google Translate'} ...")
        fallidos = ejecutar_ronda(pendientes, "ronda 1")

        # 2. Plan B: los bloques que no pasaron la validación se traducen nodo a nodo.
        if fallidos:
            fallidos_set = set(fallidos)
            nuevos_docs = []
            for rel, arbol, unidades, tipo in documentos:
                reemplazo = []
                for u in unidades:
                    if isinstance(u, UnidadElemento) and u.fuente in fallidos_set and u.slots:
                        reemplazo.extend(explotar(u))
                    else:
                        reemplazo.append(u)
                nuevos_docs.append((rel, arbol, reemplazo, tipo))
            documentos = nuevos_docs
            extra = []
            for _, _, unidades, _ in documentos:
                for u in unidades:
                    if u.fuente not in cache and u.fuente not in extra:
                        extra.append(u.fuente)
            print(f"Reintentando {len(fallidos)} bloques complejos por nodos de texto ...")
            fallidos_globales = ejecutar_ronda(extra, "ronda 2")

        # 3. Aplicar traducciones y ajustar idioma.
        sin_traducir, errores = 0, []
        for rel, arbol, unidades, tipo in documentos:
            for u in unidades:
                t = cache.get(u.fuente)
                if t is None:
                    sin_traducir += 1
                    continue
                try:
                    u.aplicar(t)
                except Exception as e:  # el bloque queda en el idioma original
                    errores.append(f"{rel}: {e}")
            if tipo == "opf":
                ajustar_opf(arbol, idioma_origen, args.idioma, args.traductor)
            else:
                ajustar_idioma(arbol, idioma_origen, args.idioma)
            escribir_archivo(arbol, nuevo / rel, tipo == "xhtml")

        # 4. Verificación estructural contra el original.
        diferencias = []
        for rel, _, _, tipo in documentos:
            if tipo == "opf":
                continue
            a = firma_estructural(parsear_archivo(original / rel))
            b = firma_estructural(parsear_archivo(nuevo / rel))
            if a != b:
                diferencias.append(rel)
        for nombre in nombres:
            if not nombre.endswith("/") and not (nuevo / nombre).exists():
                diferencias.append(f"falta {nombre}")

        empaquetar(nuevo, nombres, salida)

        informe = {
            "salida": str(salida),
            "segmentos": len(orden),
            "sin_traducir": sin_traducir,
            "fallidos": fallidos_globales,
            "errores_al_aplicar": errores,
            "archivos_con_cambios_estructurales": diferencias,
            "uso": dict(traductor.uso),
        }
        ruta_informe = salida.with_suffix(".informe.json")
        ruta_informe.write_text(json.dumps(informe, ensure_ascii=False, indent=2), "utf-8")

        print(f"\nListo: {salida}")
        print(f"  Segmentos sin traducir: {sin_traducir}  |  errores: {len(errores)}  |  "
              f"archivos con cambios de estructura: {len(diferencias)}")
        if traductor.uso:
            print(f"  Uso de Google: {dict(traductor.uso)}")
        print(f"  Informe: {ruta_informe}")

        if args.epubcheck:
            print("Validando con EPUBCheck ...")
            subprocess.run(["java", "-jar", args.epubcheck, str(salida)])
    finally:
        shutil.rmtree(trabajo, ignore_errors=True)


if __name__ == "__main__":
    main()
