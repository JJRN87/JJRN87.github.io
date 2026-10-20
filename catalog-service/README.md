# Catálogo público de recarga de GasHunter

Fuente: [RIPREE, MITECO](https://energia.serviciosmin.gob.es/Ripree/ExportarInstalaciones/Export).
Reutilización bajo las [condiciones de MITECO](https://www.datosabiertos.miteco.gob.es/es/aviso-legal.html).
La transformación conserva la procedencia, las fechas de modificación originales y el significado
de los datos; es un subconjunto normalizado y no constituye una verificación de las instalaciones.

El workflow consulta el CSV oficial una vez al día, a las 03:23 UTC, y admite ejecución manual.
GitHub puede retrasar ejecuciones programadas. Una descarga correcta no significa que los operadores
hayan actualizado los registros ese día. Los fallos de descarga o validación conservan la publicación anterior.

La app descarga el manifiesto pequeño y solo obtiene el fichero comprimido cuando cambia su SHA-256.
El manifiesto y el fichero se publican en el mismo commit; los ficheros anteriores se conservan
para permitir que las cachés intermedias sirvan una pareja coherente.
Se utiliza raw.githubusercontent.com porque los commits de GITHUB_TOKEN no activan la compilación de Pages.
No se requiere clave de API ni se reciben coordenadas, rutas o identificadores de usuarios.

Se conservan solo instalaciones con ubicación válida, acceso público no contradicho por el nombre
y al menos una franja de apertura interpretable. Un día ambiguo o ausente no se convierte en 24 horas,
ni se declara cerrado: queda como horario no confirmado. La app muestra únicamente las franjas conocidas.
No se infieren horarios del cargador a partir de la apertura de una tienda.
El campo ACCESIBILIDAD describe adaptación para personas con discapacidad, no acceso público.
La ocupación, averías, tarifas, festivos y condiciones particulares no disponibles permanecen sin confirmar.

## Uso

```sh
python3 -m unittest discover -s catalog-service -p 'test_*.py' -v
python3 catalog-service/ripree_catalog.py --output catalog/ripree/v1
```

El esquema v2 del catálogo no incluye la fecha de descarga, para mantener el hash estable
si los registros no cambian. La fecha de consulta oficial pertenece al manifiesto v1.
Nunca se modifica esa fecha por una descarga del teléfono o por una respuesta HTTP 304.
La app conserva una copia privada y una copia de emergencia incluida en el paquete.
Pasados 30 días desde la consulta oficial, deja de usar ese catálogo para mostrar puntos abiertos.

## Mantenimiento

Revisar los fallos en Actions y ejecutar manualmente «Actualizar catálogo RIPREE» tras corregirlos.
Una caída superior al 25 % o un catálogo menor de 1.000 instalaciones requiere investigar
la fuente y el normalizador; no desactivar estas barreras para forzar una publicación.
No añadir claves de REVE, credenciales, archivos Android ni exportaciones de diagnóstico a este repositorio.
