import csv
import io
import re
import openpyxl

def detect_delimiter(first_chunk: str) -> str:
    """Detecta si el archivo CSV usa punto y coma, coma o tabulación."""
    first_line = first_chunk.split("\n")[0] if first_chunk else ""
    counts = {
        ";": first_line.count(";"),
        ",": first_line.count(","),
        "\t": first_line.count("\t")
    }
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ";"

def normalize_key(key: str) -> str:
    """Normaliza encabezados para mapeo flexible."""
    k = (key or "").strip().lower()
    k = re.sub(r'[\s_]+', '', k)
    k = k.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u").replace("ñ", "n")
    return k

def map_row_fields(raw_row: dict) -> dict:
    """
    Mapea campos estándar a partir de las columnas detectadas.
    Soporta formato Moodle (firstname, lastname, email, username, password) y formatos en español.
    """
    mapped = {
        "firstname": "",
        "lastname": "",
        "nombre_completo": "",
        "email": "",
        "username": "",
        "password": "",
        "documento": "",
        "tipo_documento": "",
        "telefono": "",
        "curso": "",
        "ciudad": "",
        "_raw": raw_row
    }

    normalized = {normalize_key(k): (v.strip() if isinstance(v, str) else str(v or "")) for k, v in raw_row.items()}

    # Email
    for key in ["email", "correo", "correoelectronico", "mail"]:
        if key in normalized and normalized[key]:
            mapped["email"] = normalized[key]
            break

    # Firstname & Lastname
    for key in ["firstname", "nombre", "nombres", "primernombre"]:
        if key in normalized and normalized[key]:
            mapped["firstname"] = normalized[key]
            break

    for key in ["lastname", "apellido", "apellidos", "primerapellido"]:
        if key in normalized and normalized[key]:
            mapped["lastname"] = normalized[key]
            break

    if not mapped["firstname"] and "nombrecompleto" in normalized:
        parts = normalized["nombrecompleto"].split(maxsplit=1)
        mapped["firstname"] = parts[0]
        mapped["lastname"] = parts[1] if len(parts) > 1 else ""

    mapped["nombre_completo"] = f"{mapped['firstname']} {mapped['lastname']}".strip()

    # Username / Usuario
    for key in ["username", "usuario", "user", "login"]:
        if key in normalized and normalized[key]:
            mapped["username"] = normalized[key]
            break

    # Password / Contraseña
    for key in ["password", "contrasena", "clave", "pass"]:
        if key in normalized and normalized[key]:
            mapped["password"] = normalized[key]
            break

    # Documento de Identidad
    for key in ["profilefielddocumentoidentificacion", "documento", "documentoidentificacion", "cedula", "identificacion", "numdocumento", "nit"]:
        if key in normalized and normalized[key]:
            mapped["documento"] = normalized[key]
            break
    if not mapped["documento"] and mapped["username"] and mapped["username"].isdigit():
        mapped["documento"] = mapped["username"]

    # Tipo Documento
    for key in ["profilefieldtipodocumento", "tipodocumento", "tipodoc"]:
        if key in normalized and normalized[key]:
            mapped["tipo_documento"] = normalized[key]
            break

    # Curso
    for key in ["course1", "curso", "curso1", "nombrecurso", "programa"]:
        if key in normalized and normalized[key]:
            mapped["curso"] = normalized[key]
            break

    # Teléfono
    for key in ["phone1", "telefono", "celular", "movil", "phone"]:
        if key in normalized and normalized[key]:
            mapped["telefono"] = normalized[key]
            break

    # Ciudad
    for key in ["city", "ciudad", "municipio"]:
        if key in normalized and normalized[key]:
            mapped["ciudad"] = normalized[key]
            break

    return mapped

def parse_file(file_bytes: bytes, filename: str) -> list:
    """
    Parsea archivos CSV o Excel (xlsx/xls) y devuelve la lista de registros mapeados.
    """
    rows = []
    fname = filename.lower()

    if fname.endswith(".csv"):
        # Detectar codificación común en Windows y Moodle
        decoded = None
        for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
            try:
                decoded = file_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue

        if decoded is None:
            decoded = file_bytes.decode("utf-8", errors="replace")

        delimiter = detect_delimiter(decoded[:2048])
        reader = csv.DictReader(io.StringIO(decoded), delimiter=delimiter)
        for idx, r in enumerate(reader, start=1):
            if any(r.values()):
                mapped = map_row_fields(r)
                mapped["_id"] = idx
                rows.append(mapped)

    elif fname.endswith(".xlsx") or fname.endswith(".xls"):
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        sheet = wb.active
        headers = []
        for cell in sheet[1]:
            val = str(cell.value or "").strip()
            headers.append(val if val else f"col_{len(headers)}")

        for idx, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=1):
            if not any(row):
                continue
            raw_dict = {}
            for col_idx, val in enumerate(row):
                if col_idx < len(headers):
                    raw_dict[headers[col_idx]] = str(val if val is not None else "").strip()
            mapped = map_row_fields(raw_dict)
            mapped["_id"] = idx
            rows.append(mapped)

    return rows
