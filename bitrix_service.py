import requests
import json
import logging
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

class BitrixService:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url.rstrip("/") + "/" if webhook_url else ""

    def _call(self, method: str, data: dict = None):
        if not self.webhook_url:
            raise ValueError("URL de Webhook de Bitrix24 no configurada.")
        
        endpoint = f"{self.webhook_url}{method}.json"
        response = requests.post(endpoint, json=data or {}, timeout=15, verify=False)
        
        if response.status_code != 200:
            try:
                err_data = response.json()
                msg = err_data.get("error_description") or err_data.get("error") or response.text
            except Exception:
                msg = f"HTTP {response.status_code}: {response.text}"
            raise Exception(f"Error Bitrix24 ({method}): {msg}")
            
        res_json = response.json()
        if "error" in res_json:
            raise Exception(f"Error Bitrix24: {res_json.get('error_description', res_json['error'])}")
        return res_json.get("result")

    def test_connection(self):
        """Verifica la conectividad con Bitrix24 obteniendo información del usuario o los campos del CRM."""
        try:
            user = self._call("profile")
            name = f"{user.get('NAME', '')} {user.get('LAST_NAME', '')}".strip()
            return {
                "success": True, 
                "message": f"Conexión exitosa con Bitrix24. Usuario autenticado: {name}",
                "user": user
            }
        except Exception as e1:
            try:
                fields = self._call("crm.deal.fields")
                if fields:
                    return {
                        "success": True, 
                        "message": "Conexión exitosa con el CRM de Bitrix24."
                    }
            except Exception as e2:
                return {
                    "success": False, 
                    "message": f"Fallo al conectar: {str(e2) if str(e2) else str(e1)}"
                }

    def get_categories(self):
        """Obtiene las direcciones / embudos de ventas (Pipelines) de Negociaciones."""
        try:
            categories = self._call("crm.dealcategory.list")
            res = [{"ID": 0, "NAME": "Embudo General (Por defecto)"}]
            if isinstance(categories, list):
                for cat in categories:
                    res.append({"ID": int(cat.get("ID")), "NAME": cat.get("NAME")})
            return res
        except Exception as e:
            logger.warning(f"Error obteniendo categorías: {e}")
            return [{"ID": 99, "NAME": "TEAM"}, {"ID": 0, "NAME": "Embudo General"}]

    def get_stages(self, category_id=99):
        """Obtiene las etapas de un embudo determinado."""
        try:
            cat_id = int(category_id)
            if cat_id > 0:
                stages = self._call("crm.dealcategory.stage.list", {"id": cat_id})
                if stages and isinstance(stages, list):
                    return [{"STATUS_ID": s.get("STATUS_ID"), "NAME": s.get("NAME")} for s in stages]
            
            all_statuses = self._call("crm.status.list", {"filter": {"ENTITY_ID": "DEAL_STAGE"}})
            if all_statuses and isinstance(all_statuses, list):
                return [{"STATUS_ID": s.get("STATUS_ID"), "NAME": s.get("NAME")} for s in all_statuses]
            return []
        except Exception as e:
            logger.warning(f"No se pudieron cargar etapas: {e}")
            return [
                {"STATUS_ID": "C99:NEW", "NAME": "En desarrollo (C99:NEW)"},
                {"STATUS_ID": "C99:EXECUTING", "NAME": "NOTIFICADO (C99:EXECUTING)"},
                {"STATUS_ID": "C99:WON", "NAME": "Negociación ganada (C99:WON)"}
            ]

    def find_or_create_contact(self, first_name: str, last_name: str, email: str, phone: str = "", doc: str = ""):
        """Busca un contacto existente por email; si no existe, lo crea con campos válidos."""
        first_name = (first_name or "Sin Nombre").strip()
        last_name = (last_name or ".").strip()
        email = (email or "").strip().lower()

        if email:
            try:
                existing = self._call("crm.contact.list", {
                    "filter": {"EMAIL": email},
                    "select": ["ID", "NAME", "LAST_NAME", "EMAIL"]
                })
                if existing and len(existing) > 0:
                    return int(existing[0]["ID"])
            except Exception as e:
                logger.warning(f"Error buscando contacto: {e}")

        # Teléfono obligatorio en Bitrix24 de ATCAL
        valid_phone = phone.strip() if (phone and len(str(phone).strip()) >= 7) else "3000000000"

        fields = {
            "NAME": first_name,
            "SECOND_NAME": ".",
            "LAST_NAME": last_name if last_name else ".",
            "OPENED": "Y",
            "TYPE_ID": "CLIENT",
            "SOURCE_ID": "WEB",
            "PHONE": [{"VALUE": valid_phone, "VALUE_TYPE": "WORK"}]
        }
        if email:
            fields["EMAIL"] = [{"VALUE": email, "VALUE_TYPE": "WORK"}]
        if doc:
            fields["COMMENTS"] = f"Identificación: {doc}"

        try:
            new_contact_id = self._call("crm.contact.add", {"fields": fields})
            return int(new_contact_id)
        except Exception as e:
            logger.warning(f"Error al crear contacto (se continuará con la negociación): {e}")
            return None

    def create_deal(self, title: str, contact_id: int = None, category_id: int = 99, stage_id: str = "C99:NEW", comments: str = "", custom_fields: dict = None):
        """Crea una negociación (Deal) en Bitrix24."""
        fields = {
            "TITLE": title,
            "STAGE_ID": stage_id or "C99:NEW",
            "CATEGORY_ID": int(category_id or 99),
            "OPENED": "Y"
        }
        if contact_id:
            fields["CONTACT_ID"] = contact_id
        if comments:
            fields["COMMENTS"] = comments

        if custom_fields and isinstance(custom_fields, dict):
            for k, v in custom_fields.items():
                if k.startswith("UF_CRM_"):
                    fields[k] = v

        deal_id = self._call("crm.deal.add", {"fields": fields})
        return int(deal_id)

    def update_deal_stage(self, deal_id: int, stage_id: str):
        """Mueve la negociación a otra etapa para disparar los robots de cambio de etapa de Bitrix24."""
        return self._call("crm.deal.update", {
            "id": deal_id,
            "fields": {
                "STAGE_ID": stage_id
            }
        })

    def send_email_activity(self, deal_id: int, recipient_email: str, recipient_name: str, subject: str, html_body: str):
        """
        Crea una actividad de tipo Correo Saliente en Bitrix24 vinculado al Deal.
        """
        fields = {
            "OWNER_TYPE_ID": 2,  # 2 = Deal
            "OWNER_ID": deal_id,
            "TYPE_ID": 4,        # 4 = Email
            "SUBJECT": subject,
            "START_TIME": None,
            "END_TIME": None,
            "COMPLETED": "Y",
            "DIRECTION": 2,      # 2 = Saliente (Outgoing)
            "DESCRIPTION": html_body,
            "DESCRIPTION_TYPE": 3,
            "COMMUNICATIONS": [
                {
                    "VALUE": recipient_email,
                    "ENTITY_ID": deal_id,
                    "ENTITY_TYPE_ID": 2
                }
            ]
        }
        activity_id = self._call("crm.activity.add", {"fields": fields})
        return activity_id
