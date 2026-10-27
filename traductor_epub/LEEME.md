# Traducir un EPUB al español conservando todo el formato

`traducir_epub.py` traduce solo el texto de un EPUB (versión 2 o 3) usando
**Google Translate gratis** (mediante la librería `deep-translator`): no necesitas
clave de API ni pagas nada. Todo lo demás queda igual: nombres de archivo,
imágenes, CSS, fuentes, índice (NCX y `nav.xhtml`), enlaces internos, anclas,
notas al pie y orden de lectura.

> Úsalo con libros de tu autoría, de dominio público o para los que tengas
> licencia de traducción.

## 1. Preparación (una sola vez)

```bash
pip install deep-translator lxml
```

No hace falta ninguna clave ni variable de entorno.

## 2. Flujo de trabajo recomendado

1. **Estimar el volumen**: cuenta los segmentos y las peticiones. No se conecta a Google.
   ```bash
   python traducir_epub.py MiLibro.epub --estimar
   ```
2. **Prueba de estructura sin conexión**: genera un EPUB con el prefijo «[ES]» en
   cada texto para comprobar que no se pierde nada.
   ```bash
   python traducir_epub.py MiLibro.epub --simulado -o prueba_estructura.epub
   ```
   Ábrelo en tu lector (Calibre, Apple Books, Thorium) y revisa el índice, las
   imágenes y los enlaces. Después borra `prueba_estructura.progreso.json`.
3. **Glosario**: copia `glosario_ejemplo.json` y pon los nombres, lugares y términos
   recurrentes del libro en `terminos`. Google no acepta instrucciones de estilo,
   así que el campo `notas` se ignora.
4. **Traducción real**:
   ```bash
   python traducir_epub.py MiLibro.epub --glosario glosario.json --traductor "Tu Nombre"
   ```
5. **Revisión**: lee el resultado. Si quieres corregir un pasaje, edita su
   traducción en `MiLibro_es.progreso.json` y vuelve a correr el comando. Lo que ya
   está en ese archivo no se vuelve a pedir a Google.
6. **Validación final (opcional)**: descarga EPUBCheck desde
   <https://github.com/w3c/epubcheck/releases> y ejecuta:
   ```bash
   python traducir_epub.py MiLibro.epub --glosario glosario.json --epubcheck epubcheck.jar
   ```

## 3. Cómo funciona

| Paso | Qué hace |
|---|---|
| Lectura | Descomprime el EPUB y localiza el OPF desde `META-INF/container.xml`. Sigue el orden del *spine*. |
| Segmentación | Cada bloque (párrafo, título, elemento de lista, celda, pie de figura) se traduce completo para que la frase tenga sentido. Las cursivas, negritas, enlaces, `<br/>` e imágenes se sustituyen por marcadores `<t0>…</t0>`, que Google reubica según el orden de las palabras en español. |
| Peticiones | Junta varios párrafos en una sola petición, uno por línea, con un máximo de 4900 caracteres (Google no acepta 5000 o más). Si Google une o separa líneas, divide el grupo en mitades y reintenta. Un párrafo más largo que el límite se parte por frases. |
| Glosario | Antes de enviar el texto, cada término se reemplaza por una clave (`ZXQ0`, `ZXQ1`…) que Google deja intacta. Al volver, la clave se cambia por tu traducción, con mayúscula si queda al inicio de una frase. |
| Intocables | `code`, `pre`, `script`, `style`, `svg` y `math` no se modifican. |
| Atributos | Traduce `alt`, `title` y `aria-label`, pero ignora los que son nombres de archivo. |
| Índice | Traduce los textos del NCX y del `nav.xhtml`. Los textos repetidos (un título de capítulo y su entrada en el índice) reciben siempre la misma traducción. |
| Metadatos | Traduce `dc:title` y `dc:description`, cambia `dc:language` y los atributos `lang` a `es-419` y, si lo pides, agrega el crédito del traductor. No toca el autor. |
| Validación | Cada respuesta se revisa: todos los marcadores deben estar una sola vez y con el mismo anidamiento. Los espacios que Google mete dentro de los marcadores (`< t0 >`) se corrigen solos. Si la respuesta sigue sin cuadrar, reintenta y, si vuelve a fallar, traduce ese bloque nodo por nodo (plan B). Así el formato nunca se rompe. |
| Errores de red | Si Google responde con error o bloquea por exceso de peticiones (429), espera 2, 4, 8, 16, 32 y 60 segundos antes de rendirse con ese lote. |
| Comprobación final | Compara el original con la traducción: mismas etiquetas, `id`, `href`, `src` y `class` en cada archivo, y ningún archivo faltante. Escribe `MiLibro_es.informe.json`. |
| Empaquetado | El archivo `mimetype` va primero y sin comprimir, como exige el estándar. El resto conserva su ruta original. |

## 4. Diferencias con la versión que usaba Claude

Google Translate es gratis, pero es una traducción automática que no recibe
instrucciones. Por eso:

- No convierte los diálogos con comillas a rayas (—), no adapta el tuteo o el
  ustedeo y no garantiza un español latinoamericano neutro. Google solo ofrece
  «español» (`es`).
- No usa el contexto de los párrafos anteriores: cada línea se traduce por separado.
- Las notas de estilo del glosario se ignoran. Los términos sí se respetan.
- La calidad literaria es menor. Conviene revisar el resultado, sobre todo los
  diálogos y los juegos de palabras.
- El servicio gratuito puede bloquearte temporalmente si haces muchas peticiones.
  Si ves errores 429, baja `--hilos` a 1 o sube `--pausa`. Como el progreso se
  guarda, puedes volver a correr el comando más tarde.

## 5. Opciones

| Opción | Por defecto | Para qué |
|---|---|---|
| `-o archivo.epub` | `<nombre>_es.epub` | Ruta de salida |
| `--idioma` | `es-419` | Idioma de los metadatos. A Google se le pide la parte principal (`es`) |
| `--origen` | el del OPF | Idioma original. Usa `auto` para que Google lo detecte |
| `--glosario` | — | JSON con `terminos` |
| `--hilos` | `2` | Peticiones simultáneas (bájalo a 1 si ves errores 429) |
| `--lote-caracteres` | `4000` | Caracteres por petición (máximo 4900) |
| `--pausa` | `0.5` | Segundos de espera antes de cada petición |
| `--traductor` | — | Agrega el crédito del traductor a los metadatos |
| `--simulado` | — | Prueba sin conexión |
| `--estimar` | — | Solo calcula el volumen |
| `--epubcheck` | — | Ruta a `epubcheck.jar` para validar |

## 6. Reanudar y corregir

El progreso se guarda después de cada lote en `<salida>.progreso.json`. Si se
corta la luz o la conexión, ejecuta el mismo comando y continúa donde se quedó.
Para retraducir un pasaje, borra su entrada en ese archivo (o edítala a mano) y
vuelve a correr el script.
