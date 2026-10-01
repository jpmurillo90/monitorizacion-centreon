"""Monitorización Centreon · v1.1. Ejecutar: streamlit run app.py"""
import base64
import csv
import io
import json
from datetime import datetime
from html import escape
from pathlib import Path
import zipfile
import smtplib
from email.message import EmailMessage
import pandas as pd
import pytz

def esc(value):
    return escape(str(value), quote=True)

def safe_json(value):
    return json.dumps(value, ensure_ascii=True).replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')

def leer_csv(data, zona_origen='UTC'):
    avisos = []
    try:
        texto = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        texto = data.decode('cp1252')
        avisos.append('El archivo se ha leído con codificación Windows-1252.')
    try:
        rows = list(csv.reader(io.StringIO(texto), delimiter=';', strict=True))
    except csv.Error as exc:
        raise ValueError(f'CSV mal formado: {exc}') from exc
    required = ['Day', 'Time', 'Host', 'Address', 'Service', 'Status', 'Output']
    index = next((i for i, r in enumerate(rows) if set(required).issubset([v.strip() for v in r])), None)
    if index is None:
        raise ValueError('No se encuentra la cabecera esperada: ' + ';'.join(required))
    inicio, fin = 'N/A', 'N/A'
    for i, row in enumerate(rows[:index]):
        if len(row) >= 2 and row[0].strip() == 'Begin date' and row[1].strip() == 'End date' and i + 1 < index:
            values = rows[i + 1]
            if len(values) >= 2:
                inicio, fin = values[:2]
    columns = [v.strip() for v in rows[index]]
    if len(columns) != len(set(columns)):
        raise ValueError('Hay nombres de columnas duplicados en el CSV.')
    records = [r for r in rows[index+1:] if r and any(v.strip() for v in r)]
    bad = [i for i,r in enumerate(records, 1) if len(r) != len(columns)]
    if bad:
        raise ValueError(f'Hay {len(bad)} registros con un número de campos incorrecto; primer registro: {bad[0]}. No se han omitido silenciosamente.')
    df = pd.DataFrame(records, columns=columns)
    for col in required:
        df[col] = df[col].astype(str).str.strip()
    df['Status'] = df['Status'].str.upper()
    if ((df['Host'] == '') | (df['Status'] == '')).any():
        raise ValueError('Hay registros sin Host o Status. Revisa el CSV antes de generar el informe.')
    fechas = pd.to_datetime(df['Day'] + ' ' + df['Time'], format='mixed', dayfirst=True, errors='coerce')
    if fechas.isna().any():
        raise ValueError(f'Hay {int(fechas.isna().sum())} fechas no válidas en Day/Time.')
    try:
        df['Datetime'] = fechas.dt.tz_localize(zona_origen, ambiguous='raise', nonexistent='raise').dt.tz_convert('Europe/Madrid')
    except Exception as exc:
        raise ValueError('No se pueden interpretar las horas con la zona seleccionada. Para horas ambiguas por cambio de horario, exporta en UTC.') from exc
    df = df.sort_values('Datetime', kind='stable')
    if df.empty:
        avisos.append('El CSV tiene cabecera pero no contiene eventos.')
    return df, inicio, fin, avisos

def generar_informes(data, zona_origen='UTC', centro='SAT CI Puertollano', logo_bytes=None, modo_servicios='historico'):
    df, fecha_inicio, fecha_fin, avisos = leer_csv(data, zona_origen)
    logo_base64 = 'data:image/png;base64,' + base64.b64encode(logo_bytes).decode('ascii') if logo_bytes else ''
    def obtener_tipo_equipo(host):
        host_upper = str(host).upper().strip()
        prefijos_servidores = ('SSRC', 'SSRS', 'ESPUO1WP', 'ESPUO1LP', 'SCCPU', 'SKCPU', 'Z0EUW', 'TSRS')
        if host_upper.startswith(prefijos_servidores):
            return 'SERVIDOR'
        prefijos_red = ('ESPUO1SW', 'SW', 'ESPUO1FW', 'ESPUO1RT')
        if host_upper.startswith(prefijos_red):
            return 'RED'
        return 'OTROS'

    # ==========================================
    # 3. PROCESAMIENTO DE DATOS
    # ==========================================
    mask_ping = df['Service'].str.upper().isin(['PING', ''])
    ping_df = df[mask_ping]
    otros_df = df[~mask_ping]

    no_recuperados_lista = []
    recuperados_individuales = []

    for host, group in ping_df.groupby('Host'):
        went_down = False
        down_time = None
        down_output = None
        address = str(group.iloc[0]['Address'])
        tipo_equipo = obtener_tipo_equipo(host)

        for _, row in group.iterrows():
            status = str(row['Status']).strip().upper()
            if status in ['CRITICAL', 'DOWN']:
                if not went_down:
                    went_down = True
                    down_time = row['Datetime']
                    down_output = row['Output']
            elif status in ['OK', 'UP']:
                if went_down:
                    recuperados_individuales.append({
                        'Host': host, 'Address': address, 'Tipo': tipo_equipo,
                        'Caida_Dt': down_time, 'Recuperacion_Dt': row['Datetime'],
                        'Duracion_Delta': row['Datetime'] - down_time
                    })
                    went_down = False

        if went_down:
            no_recuperados_lista.append({
                'Host': host, 'Address': address, 'Tipo': tipo_equipo,
                'Caida': down_time.strftime('%d/%m/%Y %H:%M:%S'), 'Output': down_output
            })

    no_rec_servidores = [x for x in no_recuperados_lista if x['Tipo'] == 'SERVIDOR']
    no_rec_red = [x for x in no_recuperados_lista if x['Tipo'] == 'RED']
    no_rec_otros = [x for x in no_recuperados_lista if x['Tipo'] == 'OTROS']

    rec_servidores_consolidados = []
    rec_red_consolidados = []
    rec_otros_consolidados = []

    if recuperados_individuales:
        df_rec_ind = pd.DataFrame(recuperados_individuales)
        for host, g in df_rec_ind.groupby('Host'):
            num_caidas = len(g)
            tiempo_total_caido = g['Duracion_Delta'].sum()
            tipo_eq = g.iloc[0]['Tipo']

            primera_caida = g['Caida_Dt'].min().strftime('%d/%m/%Y %H:%M:%S')
            ultima_recuperacion = g['Recuperacion_Dt'].max().strftime('%d/%m/%Y %H:%M:%S')

            dur_str = str(tiempo_total_caido).split('.')[0]
            if 'days' in dur_str: dur_str = dur_str.replace('days', 'd')

            datos_consolidados = {
                'Host': host, 'Address': g.iloc[0]['Address'], 'Num_Caidas': num_caidas,
                'Primera_Caida': primera_caida, 'Ultima_Recuperacion': ultima_recuperacion, 'Duracion_Total': dur_str
            }

            if tipo_eq == 'SERVIDOR': rec_servidores_consolidados.append(datos_consolidados)
            elif tipo_eq == 'RED': rec_red_consolidados.append(datos_consolidados)
            else: rec_otros_consolidados.append(datos_consolidados)

    errores_df = otros_df[otros_df['Status'].isin(['WARNING', 'CRITICAL', 'UNKNOWN', 'DOWN'])]
    if modo_servicios == 'historico':
        ultimos_errores = errores_df.groupby(['Host', 'Service'], sort=False).tail(1).copy()
    else:
        ultimos = otros_df.groupby(['Host', 'Service'], sort=False).tail(1)
        ultimos_errores = ultimos[ultimos['Status'].isin(['WARNING', 'CRITICAL', 'UNKNOWN', 'DOWN'])].copy()

    total_sin_rec = len(no_recuperados_lista)
    total_rec = len(rec_servidores_consolidados) + len(rec_red_consolidados) + len(rec_otros_consolidados)

    # ==========================================
    # 4. COMPONENTES Y GENERADOR DE PLANTILLA HTML
    # ==========================================
    def badge_class(status):
        status = str(status).upper()
        if status in ['CRITICAL', 'DOWN']: return 'crit'
        if status in ['WARNING']: return 'warn'
        if status in ['OK', 'UP']: return 'ok'
        return 'unknown'

    def construir_filas_no_rec(lista_items):
        if not lista_items: return ""
        filas = ""
        for item in lista_items:
            filas += f"<tr class='filterable-row status-border-crit' data-origin='caidos'><td><b>{esc(item['Host'])}</b><br><span style='font-size:11px;color:#6b5a62;'>{esc(item['Address'])}</span></td><td style='color:#b00020;font-weight:bold;'>{item['Caida']}</td><td>{esc(item['Output'])}</td></tr>"
        return filas

    def construir_filas_rec(lista_items):
        if not lista_items: return ""
        filas = ""
        for item in lista_items:
            filas += f"""<tr class='filterable-row status-border-ok' data-origin='recuperados'>
                <td><b>{esc(item['Host'])}</b><br><span style='font-size:11px;color:#6b5a62;'>{esc(item['Address'])}</span></td>
                <td style='text-align:center;'><b>{item['Num_Caidas']}</b></td>
                <td><b>{item['Duracion_Total']}</b></td>
                <td>{item['Primera_Caida']}</td>
                <td><span style='color:#1f7a4d;font-weight:bold;'>{item['Ultima_Recuperacion']}</span></td>
            </tr>"""
        return filas

    def construir_filas_otros(df_errores):
        if df_errores.empty: return ""
        filas = ""
        for _, row in df_errores.iterrows():
            status = str(row['Status']).upper()
            border_cls = 'status-border-crit' if status in ['CRITICAL','DOWN'] else ('status-border-warn' if status == 'WARNING' else 'status-border-unknown')
            status_class = badge_class(row['Status'])
            filas += f"<tr class='filterable-row {border_cls}' data-origin='servicios'><td><b>{esc(row['Host'])}</b></td><td>{esc(row['Service'])}</td><td><span class='badge {status_class}'>{esc(row['Status'])}</span></td><td>{row['Datetime'].strftime('%d/%m/%Y %H:%M:%S')}</td><td>{esc(row['Output'])}</td></tr>"
        return filas

    html_logo = f'<img src="{logo_base64}" alt="Minsait Logo" style="max-height: 55px; width: auto; object-fit: contain;">' if logo_base64 else '<div style="background:#5a0d2e; color:white; font-weight:900; padding:10px 18px; border-radius:6px; font-size:20px;">MINSAIT</div>'

    def generar_html_dashboard(df_servicios_seccion3, incluir_graficos=True):
        chart_script = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.min.js"></script>' if incluir_graficos else ''
        etiqueta_servicios = 'Servicios con alertas en el periodo' if modo_servicios == 'historico' else 'Servicios con último estado de alerta'
        total_servicios = len(df_servicios_seccion3)
        filas_otros_html = construir_filas_otros(df_servicios_seccion3)

        conteo_status_servicios = df_servicios_seccion3['Status'].value_counts().to_dict() if not df_servicios_seccion3.empty else {}
        js_labels_svc = list(conteo_status_servicios.keys())
        js_values_svc = list(conteo_status_servicios.values())

        color_map = {'CRITICAL': '#b00020', 'WARNING': '#c97a00', 'UNKNOWN': '#6b5a62', 'OK': '#1f7a4d', 'DOWN': '#b00020', 'UP': '#1f7a4d'}
        js_colors_svc = [color_map.get(str(lbl).upper(), '#6b5a62') for lbl in js_labels_svc]

        all_rec = rec_servidores_consolidados + rec_red_consolidados + rec_otros_consolidados
        df_top_hosts = pd.DataFrame(all_rec).sort_values(by='Num_Caidas', ascending=False).head(5) if all_rec else pd.DataFrame()
        js_top_hosts_lbl = list(df_top_hosts['Host']) if not df_top_hosts.empty else []
        js_top_hosts_val = list(df_top_hosts['Num_Caidas']) if not df_top_hosts.empty else []

        # Configuración de fecha y hora de cabecera (España)
        zona_madrid = pytz.timezone('Europe/Madrid')
        hora_actual_madrid = datetime.now(zona_madrid).strftime('%d/%m/%Y %H:%M:%S')

        html_seccion_graficos = ""
        js_llamada_graficos = ""
        if incluir_graficos:
            html_seccion_graficos = f"""
            <div class="charts-grid">
              <div class="chart-card">
                  <h3>📊 Distribución de Alertas en Servicios</h3>
                  <div class="chart-wrapper"><canvas id="chartServicios"></canvas></div>
              </div>
              <div class="chart-card">
                  <h3>🔥 Top 5 Equipos con Mayor Reincidencia de Caídas</h3>
                  <div class="chart-wrapper"><canvas id="chartTopHosts"></canvas></div>
              </div>
            </div>
            """
            js_llamada_graficos = "renderizarGraficos();"

        return f"""
        <!doctype html>
        <html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Informe de Monitorización diaria – Centreon</title>
        {chart_script}
        <style>
        :root{{ --brand:#5a0d2e; --brand-dark:#3f0a22; --bg:#faf8f9; --text:#2b1b22; --muted:#6b5a62; --card:#ffffff; --border:#e9dde3; --ok:#1f7a4d; --warn:#c97a00; --crit:#b00020; }}
        *{{box-sizing:border-box}}
        body{{margin:0; font-family: Inter, system-ui, Segoe UI, Arial; background:var(--bg); color:var(--text);}}

        .hero{{ background: #ffffff; color:#2b1b22; padding:20px 26px; border-bottom: 3px solid var(--brand); display:flex; justify-content:space-between; align-items:center; gap:20px; box-shadow: 0 2px 8px rgba(0,0,0,.03); }}
        .hero-left{{ display:flex; align-items:center; gap:24px; }}
        .hero .title{{font-size:22px; font-weight:800; color: var(--brand-dark);}}
        .hero .meta{{color:var(--muted); font-size:13px; margin-top:4px;}}
        .hero .range-dates{{font-size:12px; font-weight: 600; color:var(--brand); margin-top:2px;}}

        .search-container {{ position: relative; }}
        .search-input {{ width: 340px; padding: 11px 16px; font-size: 14px; border: 2px solid var(--border); border-radius: 30px; background-color: #fff; outline: none; transition: all 0.25s ease; }}
        .search-input:focus {{ border-color: var(--brand); box-shadow: 0 0 0 4px rgba(90,13,46,0.1); width: 390px; }}

        .charts-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; padding: 20px; }}
        .chart-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 16px; box-shadow:0 2px 8px rgba(90,13,46,.02); min-height: 260px; display:flex; flex-direction:column; align-items:center; }}
        .chart-card h3 {{ margin: 0 0 12px 0; font-size: 15px; color: var(--brand-dark); align-self: flex-start; border-left: 3px solid var(--brand); padding-left: 8px; }}
        .chart-wrapper {{ width: 100%; max-height: 200px; display: flex; justify-content: center; }}

        .kpi-container {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; padding: 20px 20px 0 20px; }}
        .kpi-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; display: flex; flex-direction: column; box-shadow: 0 2px 6px rgba(0,0,0,0.01); cursor: pointer; transition: transform 0.2s; }}
        .kpi-card:hover {{ transform: translateY(-2px); }}
        .kpi-val {{ font-size: 28px; font-weight: 800; margin-bottom: 4px; }}
        .kpi-lbl {{ font-size: 13px; color: var(--muted); font-weight: 500; }}
        .kpi-card.kpi-down {{ border-left: 5px solid var(--crit); }} .kpi-card.kpi-down .kpi-val {{ color: var(--crit); }}
        .kpi-card.kpi-rec {{ border-left: 5px solid var(--ok); }} .kpi-card.kpi-rec .kpi-val {{ color: var(--ok); }}
        .kpi-card.kpi-svc {{ border-left: 5px solid var(--warn); }} .kpi-card.kpi-svc .kpi-val {{ color: var(--warn); }}

        .tabs-nav {{ display: flex; gap: 8px; padding: 20px 20px 0 20px; border-bottom: 1px solid var(--border); background: var(--bg); }}
        .tab-button {{ background: #ebe2e6; border: 1px solid var(--border); border-bottom: none; padding: 12px 24px; font-size: 14px; font-weight: 700; color: var(--muted); border-radius: 10px 10px 0 0; cursor: pointer; transition: all 0.2s ease; }}
        .tab-button:hover {{ background: #f2e9ed; color: var(--brand-dark); }}
        .tab-button.active-tab {{ background: var(--card); color: var(--brand); border-top: 3px solid var(--brand); padding-bottom: 13px; margin-bottom: -1px; z-index: 2; }}

        .tab-content {{ display: none; padding: 20px; }}
        .tab-content.active-content {{ display: block; }}

        .card{{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:16px 18px; box-shadow:0 2px 8px rgba(90,13,46,.04); position: relative; }}
        .card-header-flex {{ display: flex; justify-content: space-between; align-items: center; border-bottom:1px solid var(--border); padding-bottom:10px; margin-bottom: 14px; }}
        .card-header-flex h2 {{ margin:0; font-size:18px; color:var(--brand-dark); }}

        .btn-export {{ background: #fff; border: 1px solid var(--brand); color: var(--brand); padding: 6px 12px; font-size: 12px; font-weight: bold; border-radius: 6px; cursor: pointer; transition: all 0.2s; }}
        .btn-export:hover {{ background: var(--brand); color: #fff; }}

        .sub-section-title{{ font-size:13px; color:var(--brand); font-weight:bold; margin:15px 0 8px 0; display:block; text-transform: uppercase; letter-spacing: 0.5px;}}

        table{{width:100%; border-collapse:collapse; font-size:13px; background:#fff; overflow:hidden; margin-bottom:15px; border: 1px solid var(--border); border-radius: 6px;}}
        th,td{{padding:10px 12px; border-bottom:1px solid var(--border); vertical-align:top; text-align:left;}}
        th{{background:#f7eef2; color:#3f0a22; font-weight:bold;}}

        .filterable-row {{ transition: background-color 0.15s, transform 0.1s; }}
        .filterable-row:hover {{ background-color: #fcf6f8 !important; transform: scale(1.002); }}
        .status-border-crit {{ border-left: 4px solid var(--crit); }}
        .status-border-warn {{ border-left: 4px solid var(--warn); }}
        .status-border-ok {{ border-left: 4px solid var(--ok); }}
        .status-border-unknown {{ border-left: 4px solid var(--muted); }}

        tbody tr:nth-child(odd){{background:#fdfbfc}}
        .badge{{display:inline-block; font-size:11px; font-weight:bold; padding:3px 8px; border-radius:999px; color:#fff;}}
        .badge.ok{{background:var(--ok)}}
        .badge.warn{{background:var(--warn)}}
        .badge.crit{{background:var(--crit)}}
        .badge.unknown{{background:var(--muted)}}

        .empty-state-row {{ text-align: center; color: var(--muted); padding: 25px !important; font-weight: 500; font-size: 14px; background: #fff !important; }}
        .tab-content .card {{overflow-x:auto;}}
        td {{overflow-wrap:anywhere;}}
        @media(max-width:800px) {{
          .hero,.hero-left {{flex-direction:column;align-items:flex-start;}}
          .hero {{padding:18px;}} .search-container {{width:100%;}}
          .search-input,.search-input:focus {{width:100%;}}
          .kpi-container,.charts-grid {{grid-template-columns:1fr;}}
          .tabs-nav {{flex-wrap:wrap;}} .tab-button {{padding:10px;}}
          table {{min-width:680px;}} .tab-content {{padding:12px;}}
        }}
        @media print {{
          .tab-content {{display:block!important;}} .search-container,.tabs-nav,.btn-export {{display:none;}}
          .card {{overflow:visible!important;}} table {{font-size:10px;}}
        }}
        </style>
        </head>
        <body>
          <div class="hero">
            <div class="hero-left">
              <div class="logo-container">{html_logo}</div>
              <div>
                <div class="title">Dashboard de Monitorización Centreon – {esc(centro)}</div>
                <div class="range-dates">Periodo: Desde {esc(fecha_inicio)} hasta {esc(fecha_fin)}</div>
                <div class="meta">Generado el: {hora_actual_madrid} · Horas de eventos: Europe/Madrid</div>
                <div class="meta">{etiqueta_servicios}. Resultados limitados al CSV; no es monitorización en vivo. Un host puede tener recuperaciones y una caída posterior pendiente.</div>
              </div>
            </div>
            <div class="search-container">
              <input type="text" id="dashboardSearch" class="search-input" placeholder="🔍 Filtrar host..." onkeyup="filtrarDashboard()">
            </div>
          </div>

          <div class="kpi-container">
            <div class="kpi-card kpi-down" onclick="cambiarTab('tab-caidos')"><span class="kpi-val" id="kpi-val-caidos">{total_sin_rec}</span><span class="kpi-lbl">Hosts sin recuperación registrada</span></div>
            <div class="kpi-card kpi-rec" onclick="cambiarTab('tab-recuperados')"><span class="kpi-val" id="kpi-val-recuperados">{total_rec}</span><span class="kpi-lbl">Hosts con caídas recuperadas</span></div>
            <div class="kpi-card kpi-svc" onclick="cambiarTab('tab-servicios')"><span class="kpi-val" id="kpi-val-servicios">{total_servicios}</span><span class="kpi-lbl">{etiqueta_servicios}</span></div>
          </div>

          {html_seccion_graficos}

          <div class="tabs-nav">
            <button id="btn-tab-caidos" class="tab-button active-tab" onclick="cambiarTab('tab-caidos')">🔴 Caídos Activos (<span id="cnt-caidos">{total_sin_rec}</span>)</button>
            <button id="btn-tab-recuperados" class="tab-button" onclick="cambiarTab('tab-recuperados')">🟢 Recuperados (<span id="cnt-recuperados">{total_rec}</span>)</button>
            <button id="btn-tab-servicios" class="tab-button" onclick="cambiarTab('tab-servicios')">⚠️ Alertas de Servicios (<span id="cnt-servicios">{total_servicios}</span>)</button>
          </div>

          <div id="tab-caidos" class="tab-content active-content">
            <div class="card">
                <div class="card-header-flex">
                    <h2>🔴 Hosts Caídos (Ping) SIN Recuperar</h2>
                    <button class="btn-export" onclick="exportarCSV('tab-caidos', 'Hosts_Caidos_Activos')">📥 Exportar CSV</button>
                </div>
                <span class="sub-section-title sec-title-caidos">🖥️ Equipos Servidores</span>
                <table class="table-caidos">
                    <thead><tr><th width="25%">Host / IP</th><th width="20%">Hora de Caída</th><th width="55%">Detalle (Output)</th></tr></thead>
                    <tbody>{construir_filas_no_rec(no_rec_servidores)}</tbody>
                </table>
                <span class="sub-section-title sec-title-caidos">🌐 Equipos de Electrónica de Red</span>
                <table class="table-caidos">
                    <thead><tr><th width="25%">Host / IP</th><th width="20%">Hora de Caída</th><th width="55%">Detalle (Output)</th></tr></thead>
                    <tbody>{construir_filas_no_rec(no_rec_red)}</tbody>
                </table>
                <span class="sub-section-title sec-title-caidos">⚙️ Otros Componentes / Equipos</span>
                <table class="table-caidos">
                    <thead><tr><th width="25%">Host / IP</th><th width="20%">Hora de Caída</th><th width="55%">Detalle (Output)</th></tr></thead>
                    <tbody>{construir_filas_no_rec(no_rec_otros)}</tbody>
                </table>
                <div id="empty-caidos" class="empty-state-row" style="display:none;">🎉 No hay caídas pendientes registradas en este CSV.</div>
            </div>
          </div>

          <div id="tab-recuperados" class="tab-content">
            <div class="card">
                <div class="card-header-flex">
                    <h2>🟢 Hosts Caídos (Ping) RECUPERADOS</h2>
                    <button class="btn-export" onclick="exportarCSV('tab-recuperados', 'Hosts_Recuperados')">📥 Exportar CSV</button>
                </div>
                <span class="sub-section-title sec-title-rec">🖥️ Equipos Servidores</span>
                <table class="table-rec">
                    <thead><tr><th width="25%">Host / IP</th><th width="15%" style="text-align:center;">Nº Caídas</th><th width="20%">Tiempo Total Caído</th><th width="20%">Hora Caída (Primera)</th><th width="20%">Hora Recuperación (Última)</th></tr></thead>
                    <tbody>{construir_filas_rec(rec_servidores_consolidados)}</tbody>
                </table>
                <span class="sub-section-title sec-title-rec">🌐 Equipos de Electrónica de Red</span>
                <table class="table-rec">
                    <thead><tr><th width="25%">Host / IP</th><th width="15%" style="text-align:center;">Nº Caídas</th><th width="20%">Tiempo Total Caído</th><th width="20%">Hora Caída (Primera)</th><th width="20%">Hora Recuperación (Última)</th></tr></thead>
                    <tbody>{construir_filas_rec(rec_red_consolidados)}</tbody>
                </table>
                <span class="sub-section-title sec-title-rec">⚙️ Otros Componentes / Equipos</span>
                <table class="table-rec">
                    <thead><tr><th width="25%">Host / IP</th><th width="15%" style="text-align:center;">Nº Caídas</th><th width="20%">Tiempo Total Caído</th><th width="20%">Hora Caída (Primera)</th><th width="20%">Hora Recuperación (Última)</th></tr></thead>
                    <tbody>{construir_filas_rec(rec_otros_consolidados)}</tbody>
                </table>
                <div id="empty-recuperados" class="empty-state-row" style="display:none;">Ningún host registrado como recuperado en el periodo analizado.</div>
            </div>
          </div>

          <div id="tab-servicios" class="tab-content">
            <div class="card">
                <div class="card-header-flex">
                    <h2>⚠️ Otros Eventos (Servicios)</h2>
                    <button class="btn-export" onclick="exportarCSV('tab-servicios', 'Alertas_Servicios')">📥 Exportar CSV</button>
                </div>
                <table class="table-servicios">
                    <thead><tr><th width="20%">Host</th><th width="20%">Servicio</th><th width="10%">Estado</th><th width="15%">Último evento</th><th width="35%">Output del sensor</th></tr></thead>
                    <tbody>{filas_otros_html}</tbody>
                </table>
                <div id="empty-servicios" class="empty-state-row" style="display:none;">🔍 No hay alertas de servicios que coincidan con los filtros aplicados.</div>
            </div>
          </div>

          <script>
          function cambiarTab(tabId) {{
            var contents = document.getElementsByClassName("tab-content");
            for (var i = 0; i < contents.length; i++) contents[i].classList.remove("active-content");
            var buttons = document.getElementsByClassName("tab-button");
            for (var i = 0; i < buttons.length; i++) buttons[i].classList.remove("active-tab");
            document.getElementById(tabId).classList.add("active-content");
            document.getElementById("btn-" + tabId).classList.add("active-tab");
          }}

          function filtrarDashboard() {{
            var input = document.getElementById("dashboardSearch");
            var filter = input.value.toUpperCase();
            var rows = document.getElementsByClassName("filterable-row");
            var cCaidos = 0, cRecuperados = 0, cServicios = 0;

            for (var i = 0; i < rows.length; i++) {{
                var rowText = rows[i].textContent || rows[i].innerText;
                var origin = rows[i].getAttribute("data-origin");
                if (rowText.toUpperCase().indexOf(filter) > -1) {{
                    rows[i].style.display = "";
                    if(origin === "caidos") cCaidos++;
                    if(origin === "recuperados") cRecuperados++;
                    if(origin === "servicios") cServicios++;
                }} else {{ rows[i].style.display = "none"; }}
            }}

            document.getElementById("empty-caidos").style.display = (cCaidos === 0) ? "block" : "none";
            var tCaidos = document.getElementsByClassName("table-caidos");
            var sCaidos = document.getElementsByClassName("sec-title-caidos");
            for(var i=0; i<tCaidos.length; i++) tCaidos[i].style.display = (cCaidos === 0) ? "none" : "";
            for(var i=0; i<sCaidos.length; i++) sCaidos[i].style.display = (cCaidos === 0) ? "none" : "";

            document.getElementById("empty-recuperados").style.display = (cRecuperados === 0) ? "block" : "none";
            var tRec = document.getElementsByClassName("table-rec");
            var sRec = document.getElementsByClassName("sec-title-rec");
            for(var i=0; i<tRec.length; i++) tRec[i].style.display = (cRecuperados === 0) ? "none" : "";
            for(var i=0; i<sRec.length; i++) sRec[i].style.display = (cRecuperados === 0) ? "none" : "";

            document.getElementById("empty-servicios").style.display = (cServicios === 0) ? "block" : "none";
            var tSvc = document.getElementsByClassName("table-servicios");
            for(var i=0; i<tSvc.length; i++) tSvc[i].style.display = (cServicios === 0) ? "none" : "";

            document.getElementById("cnt-caidos").innerText = cCaidos;
            document.getElementById("cnt-recuperados").innerText = cRecuperados;
            document.getElementById("cnt-servicios").innerText = cServicios;
            document.getElementById("kpi-val-caidos").innerText = cCaidos;
            document.getElementById("kpi-val-recuperados").innerText = cRecuperados;
            document.getElementById("kpi-val-servicios").innerText = cServicios;
          }}

          window.onload = function() {{
              filtrarDashboard();
              {js_llamada_graficos}
          }};

          function renderizarGraficos() {{
              if (typeof Chart === 'undefined') return;
              const ctxSvc = document.getElementById('chartServicios').getContext('2d');
              if (ctxSvc) {{
                  new Chart(ctxSvc, {{
                      type: 'doughnut',
                      data: {{
                          labels: {safe_json(js_labels_svc)},
                          datasets: [{{
                              data: {safe_json(js_values_svc)},
                              backgroundColor: {safe_json(js_colors_svc)}
                          }}]
                      }},
                      options: {{ responsive: true, maintainAspectRatio: false }}
                  }});
              }}

              const ctxTop = document.getElementById('chartTopHosts').getContext('2d');
              if (ctxTop) {{
                  new Chart(ctxTop, {{
                      type: 'bar',
                      data: {{
                          labels: {safe_json(js_top_hosts_lbl)},
                          datasets: [{{
                              label: 'Número de Cortes',
                              data: {safe_json(js_top_hosts_val)},
                              backgroundColor: '#5a0d2e'
                          }}]
                      }},
                      options: {{
                          indexAxis: 'y',
                          responsive: true,
                          maintainAspectRatio: false,
                          plugins: {{ legend: {{ display: false }} }}
                      }}
                  }});
              }}
          }}

          function exportarCSV(tabId, nombreArchivo) {{
            const headers = tabId === 'tab-caidos' ? ['Host/IP','Hora Caida','Output'] :
              tabId === 'tab-recuperados' ? ['Host/IP','Num Caidas','Tiempo Total Caido','Primera Caida','Ultima Recuperacion'] :
              ['Host','Servicio','Estado','Ultimo Evento','Output'];
            const cell = value => {{
              let v = value.replace(/\\r?\\n/g, ' ');
              if (/^[=+@\\-\\t\\r]/.test(v)) v = "'" + v;
              return '"' + v.replace(/"/g, '""') + '"';
            }};
            const lines = [headers.map(cell).join(';')];
            document.querySelectorAll('#' + tabId + ' .filterable-row').forEach(row => {{
              if (row.style.display !== 'none') lines.push(Array.from(row.cells, td => cell(td.innerText)).join(';'));
            }});
            const url = URL.createObjectURL(new Blob(['\\ufeff' + lines.join('\\r\\n')], {{type:'text/csv;charset=utf-8'}}));
            const link = document.createElement('a'); link.href = url;
            link.download = nombreArchivo + '.csv'; document.body.appendChild(link); link.click(); link.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          }}
          </script>
        </body>
        </html>
        """


    fecha = datetime.now(pytz.timezone('Europe/Madrid')).strftime('%d-%m-%Y')
    full = generar_html_dashboard(ultimos_errores, incluir_graficos=True)
    normal = generar_html_dashboard(ultimos_errores[ultimos_errores['Status'] != 'UNKNOWN'], incluir_graficos=False)
    return {
        'full': full, 'normal': normal, 'fecha': fecha, 'avisos': avisos,
        'eventos': len(df), 'hosts': df['Host'].nunique(),
        'pendientes': total_sin_rec, 'recuperados': total_rec,
        'servicios': len(ultimos_errores), 'inicio': fecha_inicio, 'fin': fecha_fin,
    }

def enviar_informe_email(tipo, html_informe, nombre_archivo, centro, fecha_inicio, fecha_fin):
    """Envía un informe HTML mediante Gmail SMTP usando exclusivamente Streamlit Secrets."""
    import streamlit as st

    try:
        config = st.secrets['email']
        smtp_server = str(config.get('smtp_server', 'smtp.gmail.com')).strip()
        smtp_port = int(config.get('smtp_port', 587))
        usuario = str(config['usuario']).strip()
        password = str(config['password']).replace(' ', '')
        remitente = str(config.get('remitente', usuario)).strip()
        clave_destinos = 'destinatarios_normal' if tipo.lower() == 'normal' else 'destinatarios_full'
        destinatarios = list(config[clave_destinos])
        destinatarios = [str(x).strip() for x in destinatarios if str(x).strip()]
    except Exception as exc:
        raise RuntimeError('Falta o es incorrecta la configuración [email] en Streamlit Secrets.') from exc

    if not usuario or not password or not remitente or not destinatarios:
        raise RuntimeError('La configuración de correo está incompleta en Streamlit Secrets.')

    msg = EmailMessage()
    msg['From'] = remitente
    msg['To'] = ', '.join(destinatarios)
    msg['Subject'] = f'Informe Monitorización Centreon · {centro} · {fecha_fin}'
    msg.set_content(
        f'Se adjunta el informe {tipo.upper()} de monitorización Centreon.\n\n'
        f'Centro: {centro}\n'
        f'Periodo: {fecha_inicio} → {fecha_fin}\n\n'
        'Informe generado automáticamente desde la aplicación de Monitorización Centreon.'
    )
    msg.add_attachment(
        html_informe.encode('utf-8'),
        maintype='text',
        subtype='html',
        filename=nombre_archivo
    )

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(usuario, password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError('Gmail ha rechazado la autenticación. Revisa el usuario y la contraseña de aplicación en Secrets.') from exc
    except Exception as exc:
        raise RuntimeError(f'No se ha podido enviar el correo: {exc}') from exc

    return destinatarios


def exigir_acceso():
    """Cuenta compartida configurada exclusivamente en Streamlit Secrets."""
    import hashlib
    import hmac
    import time
    import streamlit as st

    try:
        config = st.secrets['acceso']
        usuario = config['usuario']
        clave = config['password']
        valido = isinstance(usuario, str) and isinstance(clave, str)
        valido = valido and bool(usuario.strip()) and len(clave) >= 16
        valido = valido and clave != 'CAMBIA_ESTO_POR_TU_CLAVE'
    except Exception:
        valido = False
    if not valido:
        st.title('Monitorización Centreon')
        st.info('Acceso pendiente de configuración. El administrador debe configurar [acceso] en Secrets con usuario y password (mínimo 16 caracteres).')
        st.stop()

    firma = hmac.new(clave.encode(), usuario.encode(), hashlib.sha256).hexdigest()
    if st.session_state.get('_acceso_firma') == firma:
        with st.sidebar:
            st.caption(f'Sesión: {usuario}')
            if st.button('Cerrar sesión'):
                for key in list(st.session_state):
                    del st.session_state[key]
                st.rerun()
        return

    # Elimina datos de una sesión anterior si se han cambiado las credenciales.
    if '_acceso_firma' in st.session_state:
        for key in list(st.session_state):
            del st.session_state[key]

    st.title('Monitorización Centreon')
    st.caption('Acceso del equipo · SAT CI')
    st.write('Introduce la cuenta común para acceder a los informes.')
    with st.form('form_acceso', clear_on_submit=True):
        login = st.text_input('Usuario')
        password = st.text_input('Contraseña', type='password')
        entrar = st.form_submit_button('Entrar', type='primary')
    if entrar:
        restante = st.session_state.get('_bloqueo_hasta', 0) - time.time()
        if restante > 0:
            st.error(f'Espera {int(restante) + 1} segundos antes de volver a intentarlo.')
        else:
            usuario_ok = hmac.compare_digest(login.strip().encode(), usuario.encode())
            clave_ok = hmac.compare_digest(password.encode(), clave.encode())
            if usuario_ok and clave_ok:
                st.session_state['_acceso_firma'] = firma
                st.session_state.pop('_fallos_acceso', None)
                st.session_state.pop('_bloqueo_hasta', None)
                st.rerun()
            else:
                fallos = st.session_state.get('_fallos_acceso', 0) + 1
                st.session_state['_fallos_acceso'] = fallos
                if fallos >= 5:
                    st.session_state['_bloqueo_hasta'] = time.time() + 60
                    st.session_state['_fallos_acceso'] = 0
                st.error('Usuario o contraseña incorrectos.')
    st.stop()


def main():
    import streamlit as st
    import streamlit.components.v1 as components
    st.set_page_config(page_title='Centreon · Informes CI', page_icon='📡', layout='wide')
    exigir_acceso()
    st.title('Monitorización Centreon')
    st.caption('SAT CI · Generador de informes · v1.2 · envío por email')
    with st.sidebar:
        st.header('Configuración del informe')
        centro = st.text_input('Centro / título', 'SAT CI Puertollano')
        zona = st.selectbox('Zona horaria del CSV', ['UTC', 'Europe/Madrid'], help='UTC conserva la conversión de tu script de Colab. Si Centreon ya exporta hora española, selecciona Europe/Madrid.')
        modo = st.radio('Alertas de servicios', ['Histórico del periodo', 'Último estado registrado'], help='Histórico conserva el último error de cada servicio aunque luego se recupere. Último estado solo muestra servicios cuyo último evento del CSV es una alerta.')
        logo_upload = st.file_uploader('Logo Minsait (PNG opcional)', type=['png'])
        st.caption('También puedes añadir logo_minsait.png junto a app.py.')
    st.write('Sube la exportación de eventos de Centreon para generar y descargar tus dos informes.')
    archivo = st.file_uploader('Archivo CSV de Centreon', type=['csv'])
    if archivo is None:
        st.info('FULL: incluye UNKNOWN y gráficos. NORMAL: excluye UNKNOWN en servicios y no incluye gráficos.')
        return
    logo_path = Path(__file__).with_name('logo_minsait.png')
    logo = logo_upload.getvalue() if logo_upload else (logo_path.read_bytes() if logo_path.is_file() else None)
    if logo and not logo.startswith(b'\x89PNG\r\n\x1a\n'):
        st.error('El logo debe ser un archivo PNG válido.')
        return
    if logo and len(logo) > 2 * 1024 * 1024:
        st.error('El logo debe pesar menos de 2 MB.')
        return
    try:
        with st.spinner('Procesando eventos…'):
            result = generar_informes(archivo.getvalue(), zona, centro, logo, 'historico' if modo.startswith('Histórico') else 'ultimo')
    except (ValueError, UnicodeError) as exc:
        st.error(str(exc))
        return
    for aviso in result['avisos']:
        st.warning(aviso)
    st.caption(f"Periodo indicado por Centreon: {result['inicio']} → {result['fin']} · {result['eventos']} eventos · {result['hosts']} hosts")
    a,b,c = st.columns(3)
    a.metric('Sin recuperación registrada', result['pendientes'])
    b.metric('Con caídas recuperadas', result['recuperados'])
    c.metric('Servicios con alertas (FULL)', result['servicios'])
    st.caption('El CSV refleja un periodo, no el estado en vivo. Un mismo host puede figurar en recuperados y pendientes si vuelve a caer.')
    fecha = result['fecha']
    nombres = {'normal': f'Informe_monitorización_SAT_{fecha}.html', 'full': f'Informe_monitorización_SAT_{fecha}_FULL.html'}
    a,b,c = st.columns(3)
    a.download_button('⬇ Informe NORMAL', result['normal'], file_name=nombres['normal'], mime='text/html', use_container_width=True)
    b.download_button('⬇ Informe FULL', result['full'], file_name=nombres['full'], mime='text/html', use_container_width=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for key, name in nombres.items():
            z.writestr(name, result[key])
    c.download_button('⬇ Ambos informes (ZIP)', buffer.getvalue(), file_name=f'Informes_Centreon_{fecha}.zip', mime='application/zip', use_container_width=True)

    st.subheader('Envío por email')
    e1, e2 = st.columns(2)
    if e1.button('📧 Enviar informe NORMAL', type='primary', use_container_width=True):
        try:
            with st.spinner('Enviando informe NORMAL…'):
                destinos = enviar_informe_email('normal', result['normal'], nombres['normal'], centro, result['inicio'], result['fin'])
            st.success('Informe NORMAL enviado correctamente a: ' + ', '.join(destinos))
        except RuntimeError as exc:
            st.error(str(exc))

    if e2.button('📧 Enviar informe FULL', use_container_width=True):
        try:
            with st.spinner('Enviando informe FULL…'):
                destinos = enviar_informe_email('full', result['full'], nombres['full'], centro, result['inicio'], result['fin'])
            st.success('Informe FULL enviado correctamente a: ' + ', '.join(destinos))
        except RuntimeError as exc:
            st.error(str(exc))
    st.subheader('Vista previa')
    seleccion = st.radio('Versión', ['NORMAL', 'FULL'], horizontal=True)
    if seleccion == 'FULL':
        st.caption('Los gráficos requieren acceso a jsDelivr. Las tablas siguen disponibles si se bloquea esa conexión.')
    components.html(result[seleccion.lower()], height=950, scrolling=True)
    st.caption('Los CSV se procesan en memoria durante la sesión y esta aplicación no los guarda en GitHub ni en una base de datos.')

if __name__ == '__main__':
    main()
