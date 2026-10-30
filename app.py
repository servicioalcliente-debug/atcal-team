import os
import json
import time
import queue
import logging
from flask import Flask, render_template, request, jsonify, Response, send_file
import io
import csv
from datetime import datetime

from bitrix_service import BitrixService
from data_processor import parse_file, map_row_fields

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'atcal-team-credentials-2026'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DEFAULT_CSV = os.path.join(BASE_DIR, "20261027_Contratistas.csv")

# Estado en memoria para la sesión activa
CURRENT_DATA = {
    "filename": "",
    "rows": [],
    "last_report": []
}

SEND_STATE = {
    "is_running": False,
    "should_stop": False,
    "progress": 0,
    "total": 0,
    "success_count": 0,
    "error_count": 0,
    "logs": []
}

event_queue = queue.Queue()

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error cargando config.json: {e}")
    return {
        "bitrix_webhook": "https://atcalsas.bitrix24.es/rest/16923/m3t5crhf7mrq5tl0/",
        "bitrix_category_id": 99,
        "bitrix_stage_id": "C99:NEW",
        "auto_move_stage": True,
        "target_stage_id": "C99:EXECUTING",
        "send_mode": "deal_only", # "deal_only" (robot de etapa) o "deal_and_activity" (email crm)
        "deal_title_template": "Credenciales Inducción - {nombre_completo} ({documento})",
        "create_contact": True,
        "course_start_date": "",
        "course_end_date": "",
        "course_end_time": "23:59",
        "platform_url": "https://campus.atcal.co",
        "email_subject": "Tus credenciales de acceso a la Inducción Corporativa Contratistas 2026 - TEAM / ATCAL",
        "email_body_template": """<p>Estimado/a <strong>{nombre_completo}</strong>,</p>
<p>Te damos la bienvenida al proceso de formación e inducción de <strong>TEAM</strong> con el acompañamiento de <strong>ATCAL</strong>.</p>
<p>A continuación te compartimos tus credenciales oficiales de acceso a la plataforma virtual:</p>
<div style="background-color: #f1f5f9; border-left: 5px solid #2563eb; padding: 16px; margin: 20px 0; border-radius: 6px; font-family: sans-serif;">
  <p style="margin: 6px 0;"><strong>Programa / Curso:</strong> {curso}</p>
  <p style="margin: 6px 0;"><strong>Usuario (Identificación):</strong> <span style="font-family: monospace; font-size: 1.1em; color: #1e3a8a;">{usuario}</span></p>
  <p style="margin: 6px 0;"><strong>Contraseña temporal:</strong> <span style="font-family: monospace; font-size: 1.1em; color: #1e3a8a;">{contraseña}</span></p>
  <p style="margin: 16px 0 6px 0;">
    <a href="{link_plataforma}" style="background-color: #2563eb; color: #ffffff; padding: 10px 22px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block;">Ingresar a la Plataforma</a>
  </p>
</div>
<p><strong>Recomendaciones para el ingreso:</strong></p>
<ul>
  <li>Ingresa con el usuario y contraseña indicados arriba.</li>
  <li>Si el sistema te solicita cambio de contraseña, asigna una clave segura que recuerdes fácilmente.</li>
  <li>Asegúrate de completar todos los módulos y evaluaciones correspondientes a tu inducción.</li>
</ul>
<p>Si presentas algún inconveniente con el acceso, responde directamente a este correo para brindarte asistencia técnica inmediata.</p>
<p>Atentamente,<br>
<strong>Gestión del Talento y Formación</strong><br>
Alianza TEAM - ATCAL S.A.S.</p>"""
    }

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def render_template_str(tpl: str, row: dict, config: dict) -> str:
    """Reemplaza placeholders con los datos del destinatario."""
    text = tpl
    mapping = {
        "{nombre}": row.get("firstname", ""),
        "{firstname}": row.get("firstname", ""),
        "{apellido}": row.get("lastname", ""),
        "{lastname}": row.get("lastname", ""),
        "{nombre_completo}": row.get("nombre_completo", ""),
        "{correo}": row.get("email", ""),
        "{email}": row.get("email", ""),
        "{usuario}": row.get("username", ""),
        "{username}": row.get("username", ""),
        "{contraseña}": row.get("password", ""),
        "{contrasena}": row.get("password", ""),
        "{password}": row.get("password", ""),
        "{documento}": row.get("documento", ""),
        "{curso}": row.get("curso", ""),
        "{ciudad}": row.get("ciudad", ""),
        "{link_plataforma}": config.get("platform_url", "https://campus.atcal.co")
    }
    for k, v in mapping.items():
        text = text.replace(k, str(v))
    return text

@app.route("/")
def index():
    config = load_config()
    return render_template("index.html", config=config, initial_file=CURRENT_DATA["filename"], initial_count=len(CURRENT_DATA["rows"]))

@app.route("/api/config", methods=["GET", "POST"])
def handle_config():
    if request.method == "POST":
        cfg = request.json or {}
        save_config(cfg)
        return jsonify({"success": True, "message": "Configuración guardada correctamente."})
    return jsonify(load_config())

@app.route("/api/clear-data", methods=["POST"])
def clear_data():
    CURRENT_DATA["rows"] = []
    CURRENT_DATA["filename"] = ""
    return jsonify({"success": True, "message": "Lista de contratistas vaciada."})

@app.route("/api/manual-add", methods=["POST"])
def manual_add():
    data = request.json or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"success": False, "message": "No se enviaron datos para agregar."}), 400

    new_rows = []
    start_id = len(CURRENT_DATA["rows"]) + 1
    for idx, item in enumerate(items, start=start_id):
        mapped = map_row_fields(item)
        mapped["_id"] = idx
        new_rows.append(mapped)

    CURRENT_DATA["rows"].extend(new_rows)
    if not CURRENT_DATA["filename"]:
        CURRENT_DATA["filename"] = "Ingreso manual / pegado"

    return jsonify({
        "success": True,
        "count": len(CURRENT_DATA["rows"]),
        "added": len(new_rows)
    })

@app.route("/api/load-default", methods=["POST"])
def load_default():
    if os.path.exists(DEFAULT_CSV):
        try:
            with open(DEFAULT_CSV, "rb") as f:
                content = f.read()
                CURRENT_DATA["rows"] = parse_file(content, "20261027_Contratistas.csv")
                CURRENT_DATA["filename"] = "20261027_Contratistas.csv"
            return jsonify({
                "success": True, 
                "count": len(CURRENT_DATA["rows"]), 
                "filename": CURRENT_DATA["filename"],
                "sample": CURRENT_DATA["rows"][:10]
            })
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": False, "message": "Archivo 20261027_Contratistas.csv no encontrado."}), 404

@app.route("/api/upload", methods=["POST"])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "No se envió ningún archivo."}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({"success": False, "message": "Nombre de archivo vacío."}), 400

    try:
        content = file.read()
        rows = parse_file(content, file.filename)
        CURRENT_DATA["rows"] = rows
        CURRENT_DATA["filename"] = file.filename
        return jsonify({
            "success": True,
            "filename": file.filename,
            "count": len(rows),
            "sample": rows[:10]
        })
    except Exception as e:
        logger.error(f"Error procesando archivo: {e}")
        return jsonify({"success": False, "message": f"Error al leer archivo: {str(e)}"}), 500

@app.route("/api/data", methods=["GET"])
def get_data():
    return jsonify({
        "filename": CURRENT_DATA["filename"],
        "count": len(CURRENT_DATA["rows"]),
        "rows": CURRENT_DATA["rows"]
    })

@app.route("/api/preview-email", methods=["POST"])
def preview_email():
    data = request.json or {}
    row_idx = data.get("row_index", 0)
    config = data.get("config") or load_config()

    rows = CURRENT_DATA["rows"]
    if not rows or row_idx >= len(rows):
        sample_row = {
            "firstname": "Giovana Andrea",
            "lastname": "Chaves Velasquez",
            "nombre_completo": "Giovana Andrea Chaves Velasquez",
            "email": "giochsv@gmail.com",
            "username": "1024591104",
            "password": "Alianza-2026",
            "documento": "1024591104",
            "curso": "Ind.cont 2026"
        }
    else:
        sample_row = rows[row_idx]

    subject = render_template_str(config.get("email_subject", ""), sample_row, config)
    body = render_template_str(config.get("email_body_template", ""), sample_row, config)
    deal_title = render_template_str(config.get("deal_title_template", "Credenciales - {nombre_completo}"), sample_row, config)

    return jsonify({
        "recipient": sample_row.get("email"),
        "name": sample_row.get("nombre_completo"),
        "subject": subject,
        "body_html": body,
        "deal_title": deal_title
    })

@app.route("/api/bitrix/test", methods=["POST"])
def test_bitrix():
    data = request.json or {}
    webhook = data.get("webhook") or load_config().get("bitrix_webhook")
    if not webhook:
        return jsonify({"success": False, "message": "Por favor ingresa la URL del Webhook de Bitrix24."}), 400

    service = BitrixService(webhook)
    result = service.test_connection()
    return jsonify(result)

@app.route("/api/bitrix/pipelines", methods=["POST"])
def get_bitrix_pipelines():
    data = request.json or {}
    webhook = data.get("webhook") or load_config().get("bitrix_webhook")
    if not webhook:
        return jsonify({"success": False, "message": "URL de Webhook no configurada."}), 400

    service = BitrixService(webhook)
    try:
        categories = service.get_categories()
        cat_id = data.get("category_id", 0)
        stages = service.get_stages(cat_id)
        return jsonify({
            "success": True,
            "categories": categories,
            "stages": stages
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/api/start-send", methods=["POST"])
def start_send():
    global SEND_STATE
    if SEND_STATE["is_running"]:
        return jsonify({"success": False, "message": "Ya hay un proceso de envío en ejecución."}), 400

    data = request.json or {}
    config = data.get("config") or load_config()
    webhook = config.get("bitrix_webhook")

    if not webhook:
        return jsonify({"success": False, "message": "No se puede iniciar el envío sin la URL del Webhook de Bitrix24."}), 400

    rows = CURRENT_DATA["rows"]
    if not rows:
        return jsonify({"success": False, "message": "No hay destinatarios cargados en la lista."}), 400

    selected_ids = data.get("selected_ids")
    if selected_ids and isinstance(selected_ids, list):
        rows = [r for r in rows if r.get("_id") in selected_ids]
        if not rows:
            return jsonify({"success": False, "message": "No seleccionaste a ningún destinatario para enviar."}), 400

    # Reiniciar estado
    SEND_STATE = {
        "is_running": True,
        "should_stop": False,
        "progress": 0,
        "total": len(rows),
        "success_count": 0,
        "error_count": 0,
        "logs": []
    }
    CURRENT_DATA["last_report"] = []

    # Iniciar procesamiento en hilo secundario
    import threading
    worker = threading.Thread(target=process_batch_send, args=(rows, config), daemon=True)
    worker.start()

    return jsonify({"success": True, "total": len(rows)})

@app.route("/api/stop-send", methods=["POST"])
def stop_send():
    global SEND_STATE
    SEND_STATE["should_stop"] = True
    return jsonify({"success": True, "message": "Detención solicitada."})

def process_batch_send(rows, config):
    global SEND_STATE
    webhook = config.get("bitrix_webhook")
    service = BitrixService(webhook)

    stage_id = config.get("bitrix_stage_id", "NEW")
    category_id = int(config.get("bitrix_category_id", 0))
    send_mode = config.get("send_mode", "deal_and_activity")
    create_contact_flag = config.get("create_contact", True)

    total = len(rows)

    for idx, row in enumerate(rows, start=1):
        if SEND_STATE["should_stop"]:
            SEND_STATE["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "status": "warning",
                "message": "Envío cancelado por el usuario."
            })
            break

        doc = row.get("documento", "")
        name = row.get("nombre_completo", "Sin Nombre")
        email = row.get("email", "").strip()
        user = row.get("username", "")
        pwd = row.get("password", "")
        curso = row.get("curso", "")

        deal_title = render_template_str(config.get("deal_title_template", "Credenciales Inducción - {nombre_completo}"), row, config)
        subject = render_template_str(config.get("email_subject", "Tus credenciales"), row, config)
        body = render_template_str(config.get("email_body_template", ""), row, config)

        report_entry = {
            "index": idx,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "documento": doc,
            "nombre": name,
            "email": email,
            "usuario": user,
            "curso": curso,
            "contact_id": "",
            "deal_id": "",
            "status": "error",
            "message": ""
        }

        try:
            if not email or "@" not in email:
                raise Exception("Correo electrónico inválido o ausente.")

            # 1. Contacto en Bitrix24
            contact_id = None
            if create_contact_flag:
                contact_id = service.find_or_create_contact(
                    first_name=row.get("firstname"),
                    last_name=row.get("lastname"),
                    email=email,
                    phone=row.get("telefono"),
                    doc=doc
                )
                report_entry["contact_id"] = contact_id

            # 2. Negociación (Deal) en Bitrix24
            deal_comments = f"Credenciales Inducción:\nUsuario: {user}\nContraseña: {pwd}\nCurso: {curso}\nCorreo: {email}\nDocumento: {doc}"
            
            start_date_val = config.get("course_start_date", "").strip()
            end_date_val = config.get("course_end_date", "").strip()
            end_time_val = config.get("course_end_time", "23:59").strip() or "23:59"

            custom_deal_fields = {
                "UF_CRM_5C968B0EE4E78": doc,
                "UF_CRM_5CED6B38B0BCF": email,
                "UF_CRM_5D5446B2933A3": row.get("lastname", ""),
                "UF_CRM_1754416758": curso
            }

            # Si el usuario configuró fechas del curso, se envían a los campos de fecha de Bitrix24
            if start_date_val:
                # Formato ISO para Bitrix24 datetime: YYYY-MM-DDTHH:MM:SS+03:00 o YYYY-MM-DD
                custom_deal_fields["UF_CRM_66CC906602912"] = f"{start_date_val}T00:00:00+03:00"
                custom_deal_fields["BEGINDATE"] = start_date_val
                deal_comments += f"\nFecha Inicio Curso: {start_date_val}"

            if end_date_val:
                custom_deal_fields["UF_CRM_66CC90661E562"] = f"{end_date_val}T{end_time_val}:00+03:00"
                custom_deal_fields["CLOSEDATE"] = end_date_val
                deal_comments += f"\nFecha Fin Curso: {end_date_val} {end_time_val} hrs"

            deal_id = service.create_deal(
                title=deal_title,
                contact_id=contact_id,
                category_id=category_id,
                stage_id=stage_id,
                comments=deal_comments,
                custom_fields=custom_deal_fields
            )
            report_entry["deal_id"] = deal_id

            # 3. Transición automática a siguiente etapa (para disparar el robot de Bitrix24)
            activity_info = ""
            auto_move = config.get("auto_move_stage", True)
            target_stage = config.get("target_stage_id", "C99:EXECUTING")
            if auto_move and target_stage and target_stage != stage_id:
                time.sleep(0.5)
                service.update_deal_stage(deal_id, target_stage)
                activity_info += " ➔ Movido a NOTIFICADO (Robot activado)"

            # 4. Envío o registro del correo si send_mode incluye crm_activity
            if send_mode == "deal_and_activity":
                act_id = service.send_email_activity(
                    deal_id=deal_id,
                    recipient_email=email,
                    recipient_name=name,
                    subject=subject,
                    html_body=body
                )
                activity_info += f" (Correo saliente CRM #{act_id})"

            report_entry["status"] = "success"
            report_entry["message"] = f"Negociación #{deal_id} creada exitosamente{activity_info}."
            SEND_STATE["success_count"] += 1

            SEND_STATE["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "status": "success",
                "message": f"[{idx}/{total}] {name} ({email}) -> Deal #{deal_id}{activity_info}"
            })

        except Exception as e:
            err_msg = str(e)
            report_entry["status"] = "error"
            report_entry["message"] = err_msg
            SEND_STATE["error_count"] += 1

            SEND_STATE["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "status": "error",
                "message": f"[{idx}/{total}] ERROR con {name} ({email}): {err_msg}"
            })

        CURRENT_DATA["last_report"].append(report_entry)
        SEND_STATE["progress"] = idx

        # Pausa para respetar la tasa de Bitrix24 REST (máx 2 req/segundo)
        time.sleep(0.6)

    SEND_STATE["is_running"] = False
    SEND_STATE["logs"].append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "status": "info",
        "message": f"Proceso finalizado. Exitosos: {SEND_STATE['success_count']}, Errores: {SEND_STATE['error_count']}."
    })

@app.route("/api/send-status", methods=["GET"])
def get_send_status():
    return jsonify(SEND_STATE)

@app.route("/api/export-report", methods=["GET"])
def export_report():
    report = CURRENT_DATA["last_report"]
    if not report:
        return jsonify({"success": False, "message": "No hay reportes de envío disponibles."}), 404

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "index", "timestamp", "documento", "nombre", "email", "usuario", "curso", "contact_id", "deal_id", "status", "message"
    ], delimiter=";")
    writer.writeheader()
    for r in report:
        writer.writerow(r)

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8-sig"))
    mem.seek(0)

    filename = f"Reporte_Envio_Credenciales_TEAM_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
