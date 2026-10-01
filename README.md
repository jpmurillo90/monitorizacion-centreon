# Monitorización Centreon · v1.0

Aplicación Streamlit adaptada del script de Colab para generar los dos informes de SAT CI. El código se guarda en GitHub y Streamlit ejecuta la aplicación.

## Subir a GitHub y desplegar

1. Crea un repositorio nuevo, por ejemplo `monitorizacion-centreon`.
2. Sube `app.py` y `requirements.txt` a la raíz del repositorio. Puedes subir también este README.
3. Opcional: añade `logo_minsait.png` junto a `app.py`. También se puede cargar el logo desde la aplicación.
4. En https://share.streamlit.io/ selecciona **Create app**, el repositorio, la rama correspondiente (habitualmente `main`) y `app.py` como archivo principal. Pulsa **Deploy**.
5. Abre la URL de la aplicación y sube el CSV exportado desde Centreon.

Documentación: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy

## Uso diario

- Exporta el CSV de eventos de Centreon y cárgalo en la aplicación.
- Indica el título del centro y la zona horaria de los eventos del CSV.
- Descarga NORMAL, FULL o ambos en ZIP. La vista previa permite revisar las pestañas y filtrar los resultados antes de compartir el HTML.
- Las descargas de HTML contienen el informe completo; el buscador del HTML filtra la vista y sus exportaciones CSV.

NORMAL excluye UNKNOWN de la sección de servicios y no contiene gráficos. FULL conserva UNKNOWN e incluye gráficos. Se mantiene la clasificación de servidores, electrónica de red y otros mediante los prefijos originales.

## Interpretación de resultados

Por defecto, **Histórico del periodo** conserva el comportamiento del script: muestra el último error de cada servicio, incluso cuando existe una recuperación posterior. Se ha corregido la etiqueta para que no se presente como alerta activa.

**Último estado registrado** muestra solamente los servicios cuyo último evento en el CSV es WARNING, CRITICAL, UNKNOWN o DOWN. Esto tampoco acredita su estado actual fuera del periodo exportado.

La zona predeterminada es UTC, igual que el script original; los eventos se convierten a Europe/Madrid con ajuste estacional. Si el CSV ya contiene horas de Madrid, elige Europe/Madrid. Las fechas de cabecera se reproducen tal como vienen del exportador. Las fechas Day admiten ISO o día/mes/año; formatos numéricos ambiguos se interpretan como día/mes/año. Las horas locales ambiguas en el cambio de horario se rechazan: en ese caso exporta en UTC.

Un host puede aparecer en recuperados y pendientes si tuvo una recuperación y luego otra caída. El tiempo total recuperado suma únicamente episodios cerrados dentro del CSV. Se mantiene el algoritmo original que agrupa Ping y eventos de host por Host; si ambos flujos discrepan, el resultado representa su secuencia combinada. No se infieren caídas anteriores al primer evento ni estados posteriores al último. Para el cálculo de recuperaciones exporta también los eventos OK/UP.

Los errores estructurales o fechas inválidas detienen el procesamiento en vez de descartar filas silenciosamente. Los valores del CSV se escapan antes de insertarlos en HTML.

## Ejecutar en el equipo

Recomendado: Python 3.11 o 3.12.

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Datos y alcance

No necesita Colab, Google Drive, una API de Centreon ni una base de datos. Los CSV se procesan en memoria en el servidor de Streamlit durante la sesión; la aplicación no los escribe en el repositorio ni guarda un histórico. No se incluye un sistema propio de usuarios. Configura la visibilidad y el acceso del alojamiento conforme al uso interno que necesites.

Los informes FULL cargan Chart.js desde jsDelivr: los gráficos necesitan acceso a ese dominio. Las tablas y filtros funcionan sin esa librería. El logo se incrusta dentro del HTML.

Validación realizada con eventos sintéticos: caídas, recuperaciones, recaídas, UNKNOWN, histórico frente a último estado, zonas horarias, archivo vacío y escape HTML. Falta contrastar con un CSV real de tu exportación.
