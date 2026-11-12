import os
import json
import time
import queue
import logging
from flask import Flask, render_template, request, jsonify, send_file
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

# Estado en memoria para Inscripción Inicial
CURRENT_DATA = {
    "filename": "",
    "rows": [],
    "last_report": []
}

# Estado en memoria para Recordatorios
REMINDER_DATA = {
    "filename": "",
    "rows": [],
    "last_report": []
}

SEND_STATE = {
    "is_running": False,
    "should_stop": False,
    "mode": "initial",  # "initial" o "reminder"
    "progress": 0,
    "total": 0,
    "success_count": 0,
    "error_count": 0,
    "logs": []
}

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
        "reminder_stage_id": "C99:NEW",
        "initial_stage_id": "C99:EXECUTING",
        "send_mode": "deal_only",
        "deal_title_template": "Credenciales Inducción - {nombre_completo} ({documento})",
        "create_contact": True,
        "course_start_date": "",
        "course_end_date": "",
        "course_end_time": "23:59",
        "course_title": "Inducción Corporativa para Contratistas 2026",
        "platform_url": "https://campus.atcal.co",
        "email_subject": "Tus credenciales de acceso a la Inducción Corporativa Contratistas 2026 - TEAM / ATCAL",
        "email_body_template": """<p>Estimado/a <strong>{nombre_completo}</strong>,</p>
<p>Te damos la bienvenida al proceso de formación e inducción de <strong>TEAM</strong> con el acompañamiento de <strong>ATCAL</strong>.</p>
<p>A continuación te compartimos tus credenciales oficiales de acceso a la plataforma virtual:</p>
<div style="background-color: #f1f5f9; border-left: 5px solid #2563eb; padding: 16px; margin: 20px 0; border-radius: 6px; font-family: sans-serif;">
  <p style="margin: 6px 0;"><strong>Programa / Curso:</strong> {curso}</p>
  <p style="margin: 6px 0;"><strong>Usuario (Identificación):</strong> <span style="font-family: monospace; font-size: 1.1em; color: #1e3a8a;">{usuario}</span></p>
  <p style="margin: 6px 0;"><strong>Contraseña asignada:</strong> <span style="font-family: monospace; font-size: 1.1em; color: #1e3a8a;">{contraseña}</span></p>
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
    return render_template(
        "index.html", 
        config=config, 
        initial_file=CURRENT_DATA["filename"], 
        initial_count=len(CURRENT_DATA["rows"]),
        reminder_file=REMINDER_DATA["filename"],
        reminder_count=len(REMINDER_DATA["rows"])
    )

@app.route("/api/config", methods=["GET", "POST"])
def handle_config():
    if request.method == "POST":
        cfg = request.json or {}
        save_config(cfg)
        return jsonify({"success": True, "message": "Configuración guardada correctamente."})
    return jsonify(load_config())

# ----------------- INSCIPCIÓN INICIAL DATA -----------------

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

    target = request.form.get("target", "initial")

    try:
        content = file.read()
        rows = parse_file(content, file.filename)
        
        if target == "reminder":
            REMINDER_DATA["rows"] = rows
            REMINDER_DATA["filename"] = file.filename
            return jsonify({
                "success": True,
                "target": "reminder",
                "filename": file.filename,
                "count": len(rows),
                "sample": rows[:10]
            })
        else:
            CURRENT_DATA["rows"] = rows
            CURRENT_DATA["filename"] = file.filename
            return jsonify({
                "success": True,
                "target": "initial",
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

# ----------------- RECORDATORIO DATA -----------------

@app.route("/api/reminder/data", methods=["GET"])
def get_reminder_data():
    return jsonify({
        "filename": REMINDER_DATA["filename"],
        "count": len(REMINDER_DATA["rows"]),
        "rows": REMINDER_DATA["rows"]
    })

@app.route("/api/reminder/clear", methods=["POST"])
def clear_reminder_data():
    REMINDER_DATA["rows"] = []
    REMINDER_DATA["filename"] = ""
    return jsonify({"success": True, "message": "Lista de recordatorios vaciada."})

@app.route("/api/reminder/copy-from-initial", methods=["POST"])
def copy_from_initial():
    """Permite reutilizar la lista de inscripción inicial para recordatorios si se desea."""
    if not CURRENT_DATA["rows"]:
        return jsonify({"success": False, "message": "No hay datos en la lista de Inscripción Inicial."}), 400
    
    REMINDER_DATA["rows"] = [dict(r) for r in CURRENT_DATA["rows"]]
    REMINDER_DATA["filename"] = f"Copia de: {CURRENT_DATA['filename'] or 'Inscripción Inicial'}"
    return jsonify({
        "success": True,
        "count": len(REMINDER_DATA["rows"]),
        "filename": REMINDER_DATA["filename"]
    })

@app.route("/api/reminder/manual-add", methods=["POST"])
def manual_add_reminder():
    data = request.json or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"success": False, "message": "No se enviaron datos para agregar."}), 400

    new_rows = []
    start_id = len(REMINDER_DATA["rows"]) + 1
    for idx, item in enumerate(items, start=start_id):
        mapped = map_row_fields(item)
        mapped["_id"] = idx
        new_rows.append(mapped)

    REMINDER_DATA["rows"].extend(new_rows)
    if not REMINDER_DATA["filename"]:
        REMINDER_DATA["filename"] = "Ingreso manual / pegado"

    return jsonify({
        "success": True,
        "count": len(REMINDER_DATA["rows"]),
        "added": len(new_rows)
    })

@app.route("/api/reminder/check-bitrix-status", methods=["POST"])
def check_bitrix_status():
    """Consulta rápida en Bitrix24 para verificar en qué etapa se encuentran los contratistas de la lista."""
    data = request.json or {}
    webhook = data.get("webhook") or load_config().get("bitrix_webhook")
    items = data.get("items", [])
    category_id = int(data.get("category_id") or load_config().get("bitrix_category_id", 99))

    if not webhook:
        return jsonify({"success": False, "message": "URL de Webhook no configurada."}), 400

    if not items:
        # Usar la lista actual de recordatorios
        items = REMINDER_DATA["rows"]

    if not items:
        return jsonify({"success": True, "results": {}})

    service = BitrixService(webhook)
    results = {}

    # Extraer documentos válidos
    docs = [str(r.get("documento") or r.get("username") or "").strip() for r in items]
    docs = [d for d in docs if d]

    try:
        # Consultar en lotes de 50 usando @UF_CRM_5C968B0EE4E78
        chunk_size = 50
        for i in range(0, len(docs), chunk_size):
            chunk = docs[i:i + chunk_size]
            try:
                res = service._call("crm.deal.list", {
                    "filter": {
                        "CATEGORY_ID": category_id,
                        "@UF_CRM_5C968B0EE4E78": chunk
                    },
                    "select": ["ID", "TITLE", "STAGE_ID", "UF_CRM_5C968B0EE4E78"]
                })
                if res and isinstance(res, list):
                    for deal in res:
                        doc_val = str(deal.get("UF_CRM_5C968B0EE4E78", "")).strip()
                        if doc_val:
                            stage = deal.get("STAGE_ID", "")
                            results[doc_val] = {
                                "deal_id": deal.get("ID"),
                                "stage_id": stage,
                                "stage_name": "NOTIFICADO" if stage == "C99:EXECUTING" else ("Recordatorio" if stage == "C99:NEW" else stage),
                                "status_color": "emerald" if stage == "C99:EXECUTING" else ("blue" if stage == "C99:NEW" else "slate")
                            }
            except Exception as e:
                logger.warning(f"Error consultando lote en Bitrix24: {e}")

        return jsonify({"success": True, "results": results})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# ----------------- PREVIEW & BITRIX PIPELINES -----------------

@app.route("/api/preview-email", methods=["POST"])
def preview_email():
    data = request.json or {}
    row_idx = data.get("row_index", 0)
    config = data.get("config") or load_config()

    rows = CURRENT_DATA["rows"] or REMINDER_DATA["rows"]
    if not rows or row_idx >= len(rows):
        sample_row = {
            "firstname": "Giovana Andrea",
            "lastname": "Chaves Velasquez",
            "nombre_completo": "Giovana Andrea Chaves Velasquez",
            "email": "giochsv@gmail.com",
            "username": "1024591104",
            "password": "Alianza-2026",
            "documento": "1024591104",
            "curso": "Inducción Corporativa para Contratistas 2026"
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
        cat_id = data.get("category_id", 99)
        stages = service.get_stages(cat_id)
        return jsonify({
            "success": True,
            "categories": categories,
            "stages": stages
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# ----------------- EJECUCIÓN (SEND & REMINDER) -----------------

@app.route("/api/start-send", methods=["POST"])
def start_send():
    global SEND_STATE
    if SEND_STATE["is_running"]:
        return jsonify({"success": False, "message": "Ya hay un proceso en ejecución."}), 400

    data = request.json or {}
    config = data.get("config") or load_config()
    webhook = config.get("bitrix_webhook")
    mode = data.get("mode", "initial") # "initial" o "reminder"

    if not webhook:
        return jsonify({"success": False, "message": "No se puede iniciar sin la URL del Webhook de Bitrix24."}), 400

    if mode == "reminder":
        rows = REMINDER_DATA["rows"]
        if not rows:
            return jsonify({"success": False, "message": "No hay contratistas en la lista de Recordatorios."}), 400
    else:
        rows = CURRENT_DATA["rows"]
        if not rows:
            return jsonify({"success": False, "message": "No hay contratistas cargados en la lista de Inscripción."}), 400

    selected_ids = data.get("selected_ids")
    if selected_ids and isinstance(selected_ids, list):
        rows = [r for r in rows if r.get("_id") in selected_ids]
        if not rows:
            return jsonify({"success": False, "message": "No seleccionaste a ningún destinatario con casilla marcada."}), 400

    # Reiniciar estado
    SEND_STATE = {
        "is_running": True,
        "should_stop": False,
        "mode": mode,
        "progress": 0,
        "total": len(rows),
        "success_count": 0,
        "error_count": 0,
        "logs": []
    }

    import threading
    if mode == "reminder":
        REMINDER_DATA["last_report"] = []
        worker = threading.Thread(target=process_batch_reminder, args=(rows, config), daemon=True)
    else:
        CURRENT_DATA["last_report"] = []
        worker = threading.Thread(target=process_batch_initial, args=(rows, config), daemon=True)

    worker.start()
    return jsonify({"success": True, "total": len(rows), "mode": mode})

@app.route("/api/stop-send", methods=["POST"])
def stop_send():
    global SEND_STATE
    SEND_STATE["should_stop"] = True
    return jsonify({"success": True, "message": "Detención solicitada."})

def process_batch_initial(rows, config):
    """Procesa el lote para Inscripción Inicial (Crea Deal y lo pasa a NOTIFICADO para disparar robot de bienvenida)."""
    global SEND_STATE
    webhook = config.get("bitrix_webhook")
    service = BitrixService(webhook)

    stage_id = config.get("bitrix_stage_id", "C99:NEW")
    category_id = int(config.get("bitrix_category_id", 99))
    send_mode = config.get("send_mode", "deal_only")
    create_contact_flag = config.get("create_contact", True)
    target_stage = config.get("target_stage_id", "C99:EXECUTING")
    auto_move = config.get("auto_move_stage", True)

    total = len(rows)
    course_title_cfg = (config.get("course_title") or "").strip()
    start_date_val = (config.get("course_start_date") or "").strip()
    end_date_val = (config.get("course_end_date") or "").strip()
    end_time_val = (config.get("course_end_time") or "23:59").strip() or "23:59"

    for idx, row in enumerate(rows, start=1):
        if SEND_STATE["should_stop"]:
            SEND_STATE["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "status": "warning",
                "message": "Envío de inscripción cancelado por el usuario."
            })
            break

        doc = row.get("documento", "") or row.get("username", "")
        name = row.get("nombre_completo", "Sin Nombre")
        email = row.get("email", "").strip()
        user = row.get("username", "")
        pwd = row.get("password", "")
        curso = course_title_cfg if course_title_cfg else (row.get("curso", "") or "Inducción TEAM")

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
            "action": "Inscripción Inicial",
            "status": "error",
            "message": ""
        }

        try:
            if not email or "@" not in email:
                raise Exception("Correo electrónico inválido o ausente.")

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

            deal_comments = f"Credenciales Inducción:\nUsuario: {user}\nContraseña: {pwd}\nCurso: {curso}\nCorreo: {email}\nDocumento: {doc}"

            custom_deal_fields = {
                "UF_CRM_5C968B0EE4E78": doc,
                "UF_CRM_5CED6B38B0BCF": email,
                "UF_CRM_5D5446B2933A3": row.get("lastname", ""),
                "UF_CRM_1754416758": curso,
                "UF_CRM_DEAL_1756300348138": curso,
                "ADDITIONAL_INFO": curso,
                "SOURCE_DESCRIPTION": curso
            }

            if start_date_val:
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

            activity_info = ""
            if auto_move and target_stage and target_stage != stage_id:
                time.sleep(0.4)
                service.update_deal_stage(deal_id, target_stage)
                activity_info += " ➔ Movido a NOTIFICADO (Robot activado)"

            if send_mode == "deal_and_activity":
                act_id = service.send_email_activity(
                    deal_id=deal_id,
                    recipient_email=email,
                    recipient_name=name,
                    subject=subject,
                    html_body=body
                )
                activity_info += f" (Correo CRM #{act_id})"

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
        time.sleep(0.5)

    SEND_STATE["is_running"] = False
    SEND_STATE["logs"].append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "status": "info",
        "message": f"Proceso de inscripción finalizado. Exitosos: {SEND_STATE['success_count']}, Errores: {SEND_STATE['error_count']}."
    })

def process_batch_reminder(rows, config):
    """
    Procesa el lote de Recordatorios:
    1. Si existe en Bitrix24 y está en NOTIFICADO (C99:EXECUTING):
       - Se traslada a Recordatorio (C99:NEW).
       - Se actualizan fechas y curso.
       - Dispara el robot de Recordatorio en Bitrix24.
    2. Si existe y ya está en Recordatorio (C99:NEW):
       - Se actualizan datos y se reporta que ya estaba en la etapa.
    3. Si no existe en Bitrix24:
       - Se crea el contacto y la negociación directamente en Recordatorio (C99:NEW) para que no se pierda.
    """
    global SEND_STATE
    webhook = config.get("bitrix_webhook")
    service = BitrixService(webhook)
    category_id = int(config.get("bitrix_category_id", 99))
    create_contact_flag = config.get("create_contact", True)

    total = len(rows)
    course_title_cfg = (config.get("course_title") or "").strip()
    start_date_val = (config.get("course_start_date") or "").strip()
    end_date_val = (config.get("course_end_date") or "").strip()
    end_time_val = (config.get("course_end_time") or "23:59").strip() or "23:59"

    for idx, row in enumerate(rows, start=1):
        if SEND_STATE["should_stop"]:
            SEND_STATE["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "status": "warning",
                "message": "Envío de recordatorios cancelado por el usuario."
            })
            break

        doc = str(row.get("documento", "") or row.get("username", "")).strip()
        name = row.get("nombre_completo", "Sin Nombre")
        email = (row.get("email", "") or "").strip()
        user = row.get("username", "") or doc
        pwd = row.get("password", "")
        curso = course_title_cfg if course_title_cfg else (row.get("curso", "") or "Inducción TEAM")

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
            "action": "",
            "status": "error",
            "message": ""
        }

        try:
            # Preparar campos de fechas y curso
            custom_deal_fields = {
                "UF_CRM_1754416758": curso,
                "UF_CRM_DEAL_1756300348138": curso,
                "ADDITIONAL_INFO": curso,
                "SOURCE_DESCRIPTION": curso
            }
            if start_date_val:
                custom_deal_fields["UF_CRM_66CC906602912"] = f"{start_date_val}T00:00:00+03:00"
                custom_deal_fields["BEGINDATE"] = start_date_val
            if end_date_val:
                custom_deal_fields["UF_CRM_66CC90661E562"] = f"{end_date_val}T{end_time_val}:00+03:00"
                custom_deal_fields["CLOSEDATE"] = end_date_val

            # 1. Buscar si la negociación ya existe en el embudo TEAM
            existing_deal = service.find_deal_by_doc_or_email(doc=doc, email=email, category_id=category_id)

            if existing_deal:
                deal_id = int(existing_deal["ID"])
                current_stage = existing_deal.get("STAGE_ID", "")
                report_entry["deal_id"] = deal_id
                report_entry["contact_id"] = existing_deal.get("CONTACT_ID", "")

                # Caso A: Está en NOTIFICADO (C99:EXECUTING) -> Mover a Recordatorio (C99:NEW)
                if current_stage == "C99:EXECUTING":
                    reminder_note = f"\n[Recordatorio activado {datetime.now().strftime('%Y-%m-%d %H:%M')}]"
                    service.update_deal_fields_and_stage(
                        deal_id=deal_id,
                        stage_id="C99:NEW", # Recordatorio
                        comments=reminder_note,
                        custom_fields=custom_deal_fields
                    )
                    report_entry["status"] = "success"
                    report_entry["action"] = "Movido a Recordatorio"
                    report_entry["message"] = f"Deal #{deal_id} estaba en NOTIFICADO. Movido con éxito a RECORDATORIO (Robot activado)."
                    SEND_STATE["success_count"] += 1

                    SEND_STATE["logs"].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "status": "success",
                        "message": f"[{idx}/{total}] Deal #{deal_id} - {name} ({doc}): Movido de NOTIFICADO a RECORDATORIO ➔ Robot activado."
                    })

                # Caso B: Ya está en Recordatorio (C99:NEW)
                elif current_stage == "C99:NEW":
                    service.update_deal_fields_and_stage(deal_id=deal_id, custom_fields=custom_deal_fields)
                    report_entry["status"] = "success"
                    report_entry["action"] = "Ya en Recordatorio"
                    report_entry["message"] = f"Deal #{deal_id} ya se encontraba en la etapa Recordatorio. Datos actualizados."
                    SEND_STATE["success_count"] += 1

                    SEND_STATE["logs"].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "status": "info",
                        "message": f"[{idx}/{total}] Deal #{deal_id} - {name} ({doc}): Ya estaba en RECORDATORIO."
                    })

                # Caso C: Otra etapa (por ejemplo WON, LOSE o APOLOGY)
                else:
                    service.update_deal_fields_and_stage(
                        deal_id=deal_id,
                        stage_id="C99:NEW",
                        custom_fields=custom_deal_fields
                    )
                    report_entry["status"] = "success"
                    report_entry["action"] = f"Trasladado a Recordatorio"
                    report_entry["message"] = f"Deal #{deal_id} estaba en {current_stage}. Trasladado a RECORDATORIO."
                    SEND_STATE["success_count"] += 1

                    SEND_STATE["logs"].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "status": "success",
                        "message": f"[{idx}/{total}] Deal #{deal_id} - {name} ({doc}): Trasladado desde {current_stage} a RECORDATORIO."
                    })

            else:
                # Caso D: No existe en Bitrix24 -> Crear directamente en Recordatorio
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

                deal_comments = f"Credenciales Inducción (Recordatorio):\nUsuario: {user}\nContraseña: {pwd}\nCurso: {curso}\nCorreo: {email}\nDocumento: {doc}"
                deal_title = render_template_str(config.get("deal_title_template", "Credenciales Inducción - {nombre_completo} ({documento})"), row, config)

                custom_deal_fields["UF_CRM_5C968B0EE4E78"] = doc
                custom_deal_fields["UF_CRM_5CED6B38B0BCF"] = email
                custom_deal_fields["UF_CRM_5D5446B2933A3"] = row.get("lastname", "")

                new_deal_id = service.create_deal(
                    title=deal_title,
                    contact_id=contact_id,
                    category_id=category_id,
                    stage_id="C99:NEW", # Creado directamente en Recordatorio
                    comments=deal_comments,
                    custom_fields=custom_deal_fields
                )
                report_entry["deal_id"] = new_deal_id
                report_entry["status"] = "success"
                report_entry["action"] = "Creado en Recordatorio"
                report_entry["message"] = f"No existía previamente. Se creó Deal #{new_deal_id} directamente en RECORDATORIO."
                SEND_STATE["success_count"] += 1

                SEND_STATE["logs"].append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "status": "success",
                    "message": f"[{idx}/{total}] Deal #{new_deal_id} - {name} ({doc}): Creado directamente en RECORDATORIO (Robot activado)."
                })

        except Exception as e:
            err_msg = str(e)
            report_entry["status"] = "error"
            report_entry["message"] = err_msg
            SEND_STATE["error_count"] += 1

            SEND_STATE["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "status": "error",
                "message": f"[{idx}/{total}] ERROR con {name} ({doc}): {err_msg}"
            })

        REMINDER_DATA["last_report"].append(report_entry)
        SEND_STATE["progress"] = idx
        time.sleep(0.5)

    SEND_STATE["is_running"] = False
    SEND_STATE["logs"].append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "status": "info",
        "message": f"Proceso de recordatorios finalizado. Exitosos: {SEND_STATE['success_count']}, Errores: {SEND_STATE['error_count']}."
    })

@app.route("/api/send-status", methods=["GET"])
def get_send_status():
    return jsonify(SEND_STATE)

@app.route("/api/export-report", methods=["GET"])
def export_report():
    mode = request.args.get("mode") or SEND_STATE.get("mode", "initial")
    if mode == "reminder":
        report = REMINDER_DATA["last_report"] or CURRENT_DATA["last_report"]
        prefix = "Recordatorio"
    else:
        report = CURRENT_DATA["last_report"] or REMINDER_DATA["last_report"]
        prefix = "Inscripcion"

    if not report:
        return jsonify({"success": False, "message": "No hay reportes de envío disponibles."}), 404

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "index", "timestamp", "documento", "nombre", "email", "usuario", "curso", "contact_id", "deal_id", "action", "status", "message"
    ], delimiter=";")
    writer.writeheader()
    for r in report:
        writer.writerow({
            "index": r.get("index"),
            "timestamp": r.get("timestamp"),
            "documento": r.get("documento"),
            "nombre": r.get("nombre"),
            "email": r.get("email"),
            "usuario": r.get("usuario"),
            "curso": r.get("curso"),
            "contact_id": r.get("contact_id"),
            "deal_id": r.get("deal_id"),
            "action": r.get("action", ""),
            "status": r.get("status"),
            "message": r.get("message")
        })

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8-sig"))
    mem.seek(0)

    filename = f"Reporte_{prefix}_TEAM_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
