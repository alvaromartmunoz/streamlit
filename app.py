import streamlit as st
import pandas as pd
from datetime import datetime, date, timedelta
from sqlalchemy import text
import re # Para parseo de action items
from streamlit_quill import st_quill # Para el editor de texto enriquecido
from ics import Calendar, Event # Para exportar a .ics
import altair as alt # Para gráficos
import requests # Asegúrate de tener requests: pip install requests
import json # Ya lo tienes

# --- Configuración de la Página ---
st.set_page_config(page_title=" Gestor de Notas", layout="wide", initial_sidebar_state="expanded")
st.image("https://cdn-icons-png.flaticon.com/512/10828/10828783.png", width=80)
st.title("Gestor de Notas")

# --- Conexión a la Base de Datos ---
@st.cache_resource # Cachear el objeto de conexión
def get_db_connection():
    try:
        conn = st.connection("mariadb_meetings_db", type="sql")
        return conn
    except Exception as e:
        st.error(f"Error al conectar con la base de datos: {e}")
        st.caption("Asegúrate de que MariaDB esté corriendo y que el archivo `.streamlit/secrets.toml` esté configurado correctamente.")
        st.stop()

conn = get_db_connection()

# --- Definición de la Tabla (Crear si no existe) ---
TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    meeting_date DATETIME NOT NULL,
    category VARCHAR(100) NULL,
    priority VARCHAR(20) NULL DEFAULT 'Media', 
    attendees TEXT NULL,
    summary TEXT NULL,
    action_items TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
"""

@st.cache_data(ttl=3600)
def create_table_if_not_exists_cached():
    try:
        with conn.session as s:
            s.execute(text(TABLE_SCHEMA))
            s.commit()
        return True
    except Exception as e:
        print(f"Error al crear/verificar la tabla 'meetings': {e}")
        return False

create_table_if_not_exists_cached()


# --- Funciones Auxiliares (Parseo de Tareas Modificado) ---
def parse_action_item(item_text_line: str, meeting_id=None, meeting_title=None):
    original_full_text = item_text_line.strip()
    
    status_match = re.match(r"\[(.*?)\]\s*(.*)", original_full_text)
    current_status_from_prefix = None
    text_for_parsing_parts = original_full_text # Default to original if no prefix

    if status_match:
        current_status_from_prefix = status_match.group(1).strip()
        text_for_parsing_parts = status_match.group(2).strip() # Text after the [Status] prefix

    parts = [p.strip() for p in text_for_parsing_parts.split(" - ", 2)]
    
    action = {
        "task": parts[0], 
        "assignee": None, 
        "due_date_str": None,
        "is_overdue": False, 
        "original_full_text": original_full_text, # Crucial for updates
        "text_for_reserialization": text_for_parsing_parts, # Task text without status prefix
        "meeting_id": meeting_id, 
        "meeting_title": meeting_title,
        "status": "Pendiente" # Default status
    }

    def parse_assignee_date_parts(potential_assignee_or_date, potential_date=None):
        assignee_val, due_date_str_val, is_overdue_val, status_val = None, None, False, "Pendiente"
        temp_assignee = None

        if potential_assignee_or_date:
            if potential_assignee_or_date.startswith("@"):
                temp_assignee = potential_assignee_or_date[1:]
            else:
                try:
                    dt_due = datetime.strptime(potential_assignee_or_date, "%Y-%m-%d").date()
                    due_date_str_val = potential_assignee_or_date
                    if dt_due < date.today():
                        is_overdue_val = True
                        status_val = "Vencido"
                except ValueError:
                    temp_assignee = potential_assignee_or_date # Not a date, assume assignee
        
        if temp_assignee and potential_date: # potential_assignee_or_date was an assignee, potential_date is next
            try:
                dt_due = datetime.strptime(potential_date, "%Y-%m-%d").date()
                due_date_str_val = potential_date
                if dt_due < date.today():
                    is_overdue_val = True
                    status_val = "Vencido"
            except ValueError: # potential_date was not a date, append to assignee
                 temp_assignee += " " + potential_date
        elif not temp_assignee and due_date_str_val and potential_date: # potential_assignee_or_date was a date, potential_date must be assignee
             if potential_date.startswith("@"):
                 temp_assignee = potential_date[1:]
             else:
                 temp_assignee = potential_date


        assignee_val = temp_assignee
        return assignee_val, due_date_str_val, is_overdue_val, status_val

    if len(parts) == 1: # Only task
        pass
    elif len(parts) == 2: # Task - (Assignee or Date)
        action["assignee"], action["due_date_str"], action["is_overdue"], derived_status = parse_assignee_date_parts(parts[1])
        if not current_status_from_prefix: action["status"] = derived_status
    elif len(parts) == 3: # Task - (Assignee or Date) - (Date or Assignee part)
        action["assignee"], action["due_date_str"], action["is_overdue"], derived_status = parse_assignee_date_parts(parts[1], parts[2])
        if not current_status_from_prefix: action["status"] = derived_status
    
    # Final status determination:
    if current_status_from_prefix:
        action["status"] = current_status_from_prefix
        if action["status"] in ["Completado", "Cancelado"]:
            action["is_overdue"] = False 
    elif action["due_date_str"]:
        dt_due = datetime.strptime(action["due_date_str"], "%Y-%m-%d").date()
        if dt_due < date.today():
            action["is_overdue"] = True
            action["status"] = "Vencido"
        else:
            action["status"] = "Pendiente"
    else: # No prefix, no due date
        action["status"] = "Abierto (sin fecha)"
        
    return action


# --- Funciones CRUD ---
def create_meeting_record(title: str, meeting_date: datetime, category: str, priority: str, attendees: str, summary_html: str, action_items: str):
    sql = text("""
        INSERT INTO meetings (title, meeting_date, category, priority, attendees, summary, action_items)
        VALUES (:title, :date, :category, :priority, :attendees, :summary, :actions)
    """)
    try:
        with conn.session as s:
            s.execute(sql, {
                "title": title, "date": meeting_date, "category": category, "priority": priority,
                "attendees": attendees, "summary": summary_html, "actions": action_items
            })
            s.commit()
        st.success("✅ Registro de reunión guardado exitosamente.")
        read_meeting_records.clear()
        get_distinct_categories.clear()
        get_all_action_items.clear()
        return True
    except Exception as e:
        st.error(f"❌ Error al guardar el registro: {e}")
        return False

@st.cache_data(ttl=60)
def read_meeting_records(sort_by="meeting_date", ascending=False, search_term="", date_from=None, date_to=None, filter_category="", filter_priority=""):
    order_direction = "ASC" if ascending else "DESC"
    where_clauses = []
    params = {}
    if search_term:
        search_like = f"%{search_term}%"
        where_clauses.append("(title LIKE :search_term OR attendees LIKE :search_term OR summary LIKE :search_term OR action_items LIKE :search_term OR category LIKE :search_term)")
        params["search_term"] = search_like
    if date_from:
        where_clauses.append("meeting_date >= :date_from")
        params["date_from"] = datetime.combine(date_from, datetime.min.time())
    if date_to:
        where_clauses.append("meeting_date <= :date_to")
        params["date_to"] = datetime.combine(date_to, datetime.max.time())
    if filter_category and filter_category != "Todas":
        where_clauses.append("category = :filter_category")
        params["filter_category"] = filter_category
    if filter_priority and filter_priority != "Todas":
        where_clauses.append("priority = :filter_priority")
        params["filter_priority"] = filter_priority
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    query = f"SELECT meeting_id, title, meeting_date, category, priority, attendees, summary, action_items, created_at FROM meetings {where_sql} ORDER BY {sort_by} {order_direction}"
    try:
        df = conn.query(query, params=params)
        return df
    except Exception as e:
        st.error(f"❌ Error al leer registros: {e}")
        return pd.DataFrame()

def send_google_chat_notification(webhook_url: str, task_details: dict):
    """
    Envía una notificación de recordatorio de tarea a Google Chat.

    Args:
        webhook_url: La URL del webhook de Google Chat.
        task_details: Un diccionario con los detalles de la tarea.
                      Esperados: 'task', 'due_date_str', 'meeting_title', 
                                 'assignee', 'status', 'meeting_id'
    """
    if not webhook_url or webhook_url == "TU_URL_DE_WEBHOOK_AQUÍ":
        # En una app Streamlit, podrías usar st.warning o st.error
        print("Advertencia: La URL del webhook de Google Chat no está configurada.")
        return False

    headers = {'Content-Type': 'application/json; charset=UTF-8'}

    message_title = "🔔 Recordatorio de Tarea 🔔"
    status_emoji = "➡️"
    if task_details.get('status', '').lower() == 'vencido' or task_details.get('is_overdue', False):
        message_title = "⚠️ Tarea Vencida ⚠️"
        status_emoji = "🔴"
    elif task_details.get('due_date_str') == date.today().strftime('%Y-%m-%d'):
        message_title = "📢 Tarea para Hoy 📢"
        status_emoji = "➡️"
    elif task_details.get('due_date_str') == (date.today() + timedelta(days=1)).strftime('%Y-%m-%d'):
        message_title = "💡 Tarea para Mañana 💡"
        status_emoji = "➡️"


    # Formato de tarjeta simple para Google Chat
    # Para tarjetas más avanzadas: https://developers.google.com/chat/api/guides/message-formats/cards
    payload = {
        "cardsV2": [
            {
                "cardId": "reminderCard",
                "card": {
                    "header": {
                        "title": message_title,
                        "subtitle": f"Reunión: {task_details.get('meeting_title', 'N/A')}",
                        "imageUrl": "https://cdn-icons-png.flaticon.com/512/10828/10828783.png", # Icono de tu app
                        "imageType": "CIRCLE"
                    },
                    "sections": [
                        {
                            "widgets": [
                                {
                                    "decoratedText": {
                                        "topLabel": "TAREA",
                                        "text": f"<b>{task_details.get('task', 'N/A')}</b>",
                                        "wrapText": True
                                    }
                                },
                                {
                                    "decoratedText": {
                                        "topLabel": "FECHA LÍMITE",
                                        "text": task_details.get('due_date_str', 'N/A')
                                    }
                                },
                                {
                                    "decoratedText": {
                                        "topLabel": "ASIGNADO A",
                                        "text": task_details.get('assignee', 'N/A') or "<i>Nadie</i>"
                                    }
                                },
                                {
                                    "decoratedText": {
                                        "topLabel": "ESTADO ACTUAL",
                                        "text": f"{status_emoji} {task_details.get('status', 'N/A')}"
                                    }
                                }
                                # Si tu app es pública, podrías añadir un botón con un enlace:
                                # {
                                # "buttonList": {
                                # "buttons": [
                                # {
                                # "text": "Abrir en Gestor",
                                # "onClick": {
                                # "openLink": {
                                # "url": f"URL_PUBLICA_DE_TU_APP/reunion/{task_details.get('meeting_id')}"
                                # }
                                # }
                                # }
                                # ]
                                # }
                                # }
                            ]
                        }
                    ]
                }
            }
        ]
    }

    try:
        response = requests.post(webhook_url, headers=headers, data=json.dumps(payload))
        response.raise_for_status() # Lanza una excepción para errores HTTP 4xx/5xx
        print(f"Notificación enviada a Google Chat para tarea: '{task_details.get('task')}'")
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error al enviar notificación a Google Chat: {e}")
        return False
    
def check_and_send_due_task_reminders(webhook_url_from_secrets: str):
    """
    Comprueba las tareas y envía recordatorios a Google Chat para las que estén próximas a vencer o vencidas.
    """
    if not webhook_url_from_secrets:
        print("La URL del webhook no está en los secretos. No se enviarán recordatorios.")
        return

    print(f"[{datetime.now()}] Iniciando comprobación de recordatorios...")
    all_tasks_df = get_all_action_items() # Usas tu función existente

    if all_tasks_df.empty:
        print("No se encontraron puntos de acción.")
        return

    today = date.today()
    tomorrow = today + timedelta(days=1)
    reminders_sent_count = 0

    for index, task_row in all_tasks_df.iterrows():
        task_status = task_row.get('status', '').lower()
        due_date_str = task_row.get('due_date_str')

        # No enviar recordatorios para tareas ya completadas o canceladas
        if task_status in ['completado', 'cancelada', '[completado]', '[cancelado]']:
            continue

        if due_date_str:
            try:
                due_date_obj = datetime.strptime(due_date_str, "%Y-%m-%d").date()
                
                send_reminder = False
                # Definir cuándo enviar el recordatorio:
                # 1. Si vence hoy
                if due_date_obj == today:
                    send_reminder = True
                # 2. Si vence mañana
                elif due_date_obj == tomorrow:
                    send_reminder = True
                # 3. Si está vencida (y no es una tarea antigua ya muy vencida para evitar spam,
                #    quizás solo recordar las vencidas en los últimos 7 días, o una vez por semana)
                elif due_date_obj < today:
                    days_overdue = (today - due_date_obj).days
                    # Ejemplo: Recordar tareas vencidas hasta 7 días atrás, o si es un múltiplo de 3 días vencida.
                    if days_overdue <= 7 or days_overdue % 3 == 0:
                         send_reminder = True
                
                if send_reminder:
                    # Prepara los detalles para la función de notificación
                    task_details_for_chat = {
                        'task': task_row.get('task'),
                        'due_date_str': due_date_str,
                        'meeting_title': task_row.get('meeting_title'),
                        'assignee': task_row.get('assignee'),
                        'status': task_row.get('status'), # El estado parseado
                        'meeting_id': task_row.get('meeting_id'),
                        'is_overdue': task_row.get('is_overdue', False) # El flag de parse_action_item
                    }
                    if send_google_chat_notification(webhook_url_from_secrets, task_details_for_chat):
                        reminders_sent_count += 1
                        # Podrías añadir un pequeño delay si envías muchas para no saturar
                        # import time
                        # time.sleep(1) 

            except ValueError:
                # Ignorar tareas con formato de fecha incorrecto en due_date_str
                print(f"Formato de fecha incorrecto para tarea '{task_row.get('task')}': {due_date_str}")
                continue
    
    print(f"Comprobación de recordatorios finalizada. {reminders_sent_count} recordatorios enviados/intentados.")

def update_meeting_record(meeting_id: int, title: str, meeting_date: datetime, category: str, priority: str, attendees: str, summary_html: str, action_items: str):
    sql_query = text("""
        UPDATE meetings
        SET title = :title, meeting_date = :date, category = :category, priority = :priority, attendees = :attendees,
            summary = :summary, action_items = :actions
        WHERE meeting_id = :meeting_id
    """)
    try:
        with conn.session as s:
            s.execute(sql_query, {
                "title": title, "date": meeting_date, "category": category, "priority": priority, "attendees": attendees,
                "summary": summary_html, "actions": action_items, "meeting_id": meeting_id
            })
            s.commit()
        st.success(f"✅ Registro ID {meeting_id} actualizado.")
        read_meeting_records.clear()
        get_distinct_categories.clear()
        get_all_action_items.clear()
        return True
    except Exception as e:
        st.error(f"❌ Error al actualizar registro: {e}")
        return False

def delete_meeting_record(meeting_id: int):
    sql_query = text("DELETE FROM meetings WHERE meeting_id = :meeting_id")
    try:
        with conn.session as s:
            s.execute(sql_query, {"meeting_id": meeting_id})
            s.commit()
        st.success(f"🗑️ Registro ID {meeting_id} eliminado.")
        read_meeting_records.clear()
        get_distinct_categories.clear()
        get_all_action_items.clear()
        return True
    except Exception as e:
        st.error(f"❌ Error al eliminar registro: {e}")
        return False

@st.cache_data(ttl=300)
def get_distinct_categories():
    try:
        df_categories = conn.query("SELECT DISTINCT category FROM meetings WHERE category IS NOT NULL AND category != '' ORDER BY category ASC")
        return ["Todas"] + list(df_categories['category'].unique())
    except Exception:
        return ["Todas"]

@st.cache_data(ttl=60)
def get_all_action_items():
    all_meetings = read_meeting_records(sort_by="meeting_date", ascending=False)
    action_items_list = []
    if not all_meetings.empty:
        for _, meeting in all_meetings.iterrows():
            if pd.notna(meeting['action_items']) and meeting['action_items']:
                for item_text in meeting['action_items'].split('\n'):
                    if item_text.strip():
                        parsed = parse_action_item(item_text.strip(), meeting['meeting_id'], meeting['title'])
                        action_items_list.append(parsed)
    return pd.DataFrame(action_items_list)

# ======== NUEVA FUNCIÓN PARA ACTUALIZAR ESTADO DE TAREA ========
def update_action_item_status(meeting_id: int, original_full_item_text: str, new_status: str):
    try:
        meeting_df = conn.query("SELECT action_items FROM meetings WHERE meeting_id = :id", params={"id": meeting_id}, ttl=0)
        if meeting_df.empty:
            st.error(f"No se encontró la reunión con ID {meeting_id} para actualizar la tarea.")
            return False
        
        current_action_items_text = meeting_df.iloc[0]['action_items']
        if not current_action_items_text: current_action_items_text = ""

        action_items_list = current_action_items_text.split('\n')
        new_action_items_list = []
        found_and_updated = False

        for item_line in action_items_list:
            if item_line.strip() == original_full_item_text.strip():
                parsed_original = parse_action_item(original_full_item_text) # Para obtener text_for_reserialization
                text_to_prefix = parsed_original['text_for_reserialization']
                
                if new_status == "Quitar Estado" or not new_status: # Opción para quitar el prefijo
                    new_item_line = text_to_prefix
                else:
                    new_item_line = f"[{new_status}] {text_to_prefix}"
                new_action_items_list.append(new_item_line)
                found_and_updated = True
            else:
                new_action_items_list.append(item_line)
        
        if not found_and_updated:
            # Esto podría pasar si el original_full_item_text tiene espacios extra o algo así
            # O si la tarea fue modificada por otro medio y ya no coincide exactamente.
            st.warning(f"No se encontró la tarea exacta '{original_full_item_text}' para actualizar en la reunión ID {meeting_id}. Verifique si fue modificada externamente.")
            # No se actualiza nada si no se encuentra la tarea exacta.
            return False

        updated_action_items_str = "\n".join(new_action_items_list)
        sql_update = text("UPDATE meetings SET action_items = :actions WHERE meeting_id = :id")
        with conn.session as s:
            s.execute(sql_update, {"actions": updated_action_items_str, "id": meeting_id})
            s.commit()
        
        read_meeting_records.clear()
        get_all_action_items.clear() 
        st.toast(f"Estado de tarea actualizado a '{new_status}' en reunión ID {meeting_id}.", icon="🎉")
        return True

    except Exception as e:
        st.error(f"Error al actualizar el estado de la tarea: {e}")
        return False
# =================================================================

PRIORITY_OPTIONS = ["Todas", "Alta", "Media", "Baja"]
DEFAULT_PRIORITY = "Media"

def generate_markdown_export(meeting_data):
    md = f"# {meeting_data['title']}\n\n"
    md += f"**ID:** `{meeting_data['meeting_id']}`\n"
    md += f"**Fecha:** {pd.to_datetime(meeting_data['meeting_date']).strftime('%Y-%m-%d %H:%M')}\n"
    if pd.notna(meeting_data['category']) and meeting_data['category']:
        md += f"**Categoría/Proyecto:** {meeting_data['category']}\n"
    if pd.notna(meeting_data['priority']) and meeting_data['priority']:
        md += f"**Prioridad:** {meeting_data['priority']}\n"
    md += f"**Fecha de Creación:** {pd.to_datetime(meeting_data['created_at']).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    
    md += "## 👥 Asistentes\n"
    if pd.notna(meeting_data['attendees']) and meeting_data['attendees']:
        for att in meeting_data['attendees'].split('\n'):
            if att.strip(): md += f"- {att.strip()}\n"
    else:
        md += "N/A\n"
    md += "\n"
    
    md += "## 📝 Resumen / Minuta\n"
    summary_text = meeting_data['summary']
    if pd.notna(summary_text) and summary_text:
        clean_summary = re.sub('<[^<]+?>', '', summary_text) 
        md += f"{clean_summary}\n\n"
    else:
        md += "N/A\n\n"
    
    md += "## 📌 Puntos de Acción\n"
    if pd.notna(meeting_data['action_items']) and meeting_data['action_items']:
        for item_text in meeting_data['action_items'].split('\n'):
            if item_text.strip():
                action = parse_action_item(item_text.strip()) # Usar parse_action_item modificado
                # Mostrar el estado parseado en el Markdown
                md += f"- **Tarea ({action['status']}):** {action['task']}\n"
                if action['assignee']:
                    md += f"  - **Responsable:** {action['assignee']}\n"
                if action['due_date_str']:
                    md += f"  - **Para:** {action['due_date_str']}{' (VENCIDO!)' if action['is_overdue'] and action['status'] != 'Completado' else ''}\n" # No mostrar VENCIDO si está Completado
                md += "\n"
    else:
        md += "N/A\n"
    return md

def generate_ics_export(meeting_data):
    c = Calendar(); e = Event(); e.name = meeting_data['title']
    meeting_dt = pd.to_datetime(meeting_data['meeting_date'])
    e.begin = meeting_dt; e.end = meeting_dt + timedelta(hours=1)
    description = f"Categoría: {meeting_data.get('category', 'N/A')}\nPrioridad: {meeting_data.get('priority', 'N/A')}\n"
    summary_text = meeting_data.get('summary', 'N/A')
    if pd.notna(summary_text) and summary_text:
        description += f"\nResumen:\n{re.sub('<[^<]+?>', '', summary_text)[:200]}..."
    e.description = description
    if pd.notna(meeting_data['attendees']) and meeting_data['attendees']:
        e.attendees = [att.strip() for att in meeting_data['attendees'].split('\n') if att.strip()]
    c.events.add(e); return str(c)

# --- Inicialización de Session State ---
if 'editing_meeting_id' not in st.session_state: st.session_state.editing_meeting_id = None
if 'meeting_to_edit' not in st.session_state: st.session_state.meeting_to_edit = None
if 'active_tab' not in st.session_state: st.session_state.active_tab = "view_meetings"
if 'action_items_before_edit' not in st.session_state: st.session_state.action_items_before_edit = pd.DataFrame()

# --- Interfaz de Usuario con Pestañas ---
tab_add_edit, tab_view, tab_actions, tab_stats = st.tabs(["➕ Añadir / Editar", "🗓️ Ver Registros", "🎯 Tracker de Acciones", "📊 Estadísticas"])

with tab_add_edit:
    st.subheader("✍️ Registrar Nueva Reunión" if st.session_state.editing_meeting_id is None else f"✏️ Editando Reunión ID: {st.session_state.editing_meeting_id}")
    if st.session_state.editing_meeting_id is not None and st.session_state.meeting_to_edit is None:
        try:
            df_edit = conn.query("SELECT * FROM meetings WHERE meeting_id = :id", params={"id": st.session_state.editing_meeting_id}, ttl=0)
            if not df_edit.empty: st.session_state.meeting_to_edit = df_edit.iloc[0].to_dict()
            else: 
                st.warning("No se encontró la reunión para editar."); st.session_state.editing_meeting_id = None; st.rerun()
        except Exception as e: 
            st.error(f"Error al cargar datos para edición: {e}"); st.session_state.editing_meeting_id = None; st.rerun()
    
    default_data = st.session_state.meeting_to_edit if st.session_state.editing_meeting_id else {}
    default_summary_content = default_data.get("summary", "")
    form_key = f"meeting_form_{st.session_state.editing_meeting_id or 'new'}"
    with st.form(key=form_key):
        c1, c2, c3 = st.columns(3)
        with c1:
            mt_title = st.text_input("Título*", value=default_data.get("title", ""))
            mt_date = st.date_input("Fecha*", value=pd.to_datetime(default_data.get("meeting_date")).date() if default_data.get("meeting_date") else date.today())
        with c2:
            mt_category = st.text_input("Categoría", value=default_data.get("category", ""), placeholder="Ej: Proyecto Alpha")
            mt_time = st.time_input("Hora*", value=pd.to_datetime(default_data.get("meeting_date")).time() if default_data.get("meeting_date") else datetime.now().time())
        with c3:
            current_priority = default_data.get("priority", DEFAULT_PRIORITY)
            mt_priority = st.selectbox("Prioridad*", options=PRIORITY_OPTIONS[1:], index=PRIORITY_OPTIONS[1:].index(current_priority) if current_priority in PRIORITY_OPTIONS[1:] else 0)
        mt_attendees = st.text_area("Asistentes", value=default_data.get("attendees", ""), placeholder="Uno por línea.", height=100)
        st.markdown("**Resumen / Minuta:**")
        mt_summary_html = st_quill(value=default_summary_content, placeholder="Puntos clave...", html=True, key="quill_editor")
        mt_action_items = st.text_area("Puntos de Acción", value=default_data.get("action_items", ""), height=150, placeholder="Formato: [Estado Opcional] Tarea - @Responsable - YYYY-MM-DD\nEj: [Completado] Revisar informe - @Ana - 2023-10-25")
        submitted = st.form_submit_button("💾 Guardar Cambios" if st.session_state.editing_meeting_id else "➕ Añadir Reunión", use_container_width=True, type="primary")
        if submitted:
            if not mt_title or not mt_date or not mt_time: st.warning("⚠️ Título, Fecha y Hora son obligatorios.")
            elif not mt_summary_html or mt_summary_html == "<p><br></p>": st.warning("⚠️ El resumen no puede estar vacío.")
            else:
                meeting_datetime = datetime.combine(mt_date, mt_time)
                category_val = mt_category.strip() if mt_category else None
                success = update_meeting_record(st.session_state.editing_meeting_id, mt_title, meeting_datetime, category_val, mt_priority, mt_attendees, mt_summary_html, mt_action_items) if st.session_state.editing_meeting_id else create_meeting_record(mt_title, meeting_datetime, category_val, mt_priority, mt_attendees, mt_summary_html, mt_action_items)
                if success:
                    st.session_state.editing_meeting_id = None; st.session_state.meeting_to_edit = None
                    st.session_state.active_tab = "view_meetings"; st.rerun()
    if st.session_state.editing_meeting_id and st.button("✖️ Cancelar Edición", use_container_width=True):
        st.session_state.editing_meeting_id = None; st.session_state.meeting_to_edit = None; st.rerun()

with tab_view:
    st.header("🔎 Filtrar y Visualizar Reuniones")
    categories = get_distinct_categories()
    with st.container(border=True):
        c1,c2,c3 = st.columns(3)
        with c1:
            search_query = st.text_input("🔍 Buscar:", key="search_view", placeholder="Palabra clave...")
            filter_cat = st.selectbox("Categoría:", options=categories, key="filter_cat_view")
        with c2:
            date_filter_from = st.date_input("Desde:", value=None, key="date_from_view", format="YYYY-MM-DD")
            date_filter_to = st.date_input("Hasta:", value=None, key="date_to_view", format="YYYY-MM-DD")
        with c3:
            filter_pri = st.selectbox("Prioridad:", options=PRIORITY_OPTIONS, key="filter_pri_view")
            sort_column = st.selectbox("Ordenar por:", ["meeting_date", "title", "category", "priority", "created_at"], index=0, key="sort_col_view")
            sort_order_asc = st.toggle("⬆️ Ascendente", value=False, key="sort_order_view")
        if st.button("🔄 Aplicar / Refrescar", use_container_width=True): st.rerun()

    df_meetings = read_meeting_records(sort_column, sort_order_asc, search_query, date_filter_from, date_filter_to, filter_cat, filter_pri)
    st.markdown("---")
    if df_meetings.empty: st.info("ℹ️ No se encontraron reuniones o no hay registros.")
    else:
        try: total_records_unfiltered = conn.query("SELECT COUNT(*) AS count FROM meetings", ttl=60).iloc[0]['count']
        except: total_records_unfiltered = len(df_meetings) # Fallback
        st.caption(f"Mostrando {len(df_meetings)} de {total_records_unfiltered} reunión(es).")
        priority_emoji = {"Alta": "🔥", "Media": "🔸", "Baja": "🔹"}
        for index, row in df_meetings.iterrows():
            title_display = f"{priority_emoji.get(row['priority'], '')} **{row['title']}** ({pd.to_datetime(row['meeting_date']).strftime('%d %b %Y, %H:%M')})"
            if pd.notna(row['category']) and row['category']: title_display += f"  | Cat: _{row['category']}_"
            with st.expander(title_display):
                col_details, col_actions_disp = st.columns([0.75, 0.25])
                with col_details:
                    st.markdown(f"**ID:** `{row['meeting_id']}` | **Creado:** `{pd.to_datetime(row['created_at']).strftime('%Y-%m-%d %H:%M')}`")
                    cont_col1, cont_col2 = st.columns(2)
                    with cont_col1:
                        st.subheader("👥 Asistentes")
                        attendees_list = [a.strip() for a in row['attendees'].split('\n') if a.strip()] if pd.notna(row['attendees']) and row['attendees'] else []
                        if attendees_list: [st.markdown(f"- {att}") for att in attendees_list]
                        else: st.caption("N/A")
                    with cont_col2:
                        st.subheader("📌 Puntos de Acción")
                        if pd.notna(row['action_items']) and row['action_items']:
                            for item_text in row['action_items'].split('\n'):
                                if item_text.strip():
                                    action = parse_action_item(item_text.strip())
                                    disp_text = f"{action['task']}"
                                    if action['assignee']: disp_text += f" (@{action['assignee']})"
                                    if action['due_date_str']: disp_text += f" [Para: {action['due_date_str']}]"
                                    
                                    color = "inherit"
                                    status_prefix = f" ({action['status']}) "
                                    if action['status'] == 'Completado': color = 'green'; status_prefix = " ✅ "
                                    elif action['status'] == 'Cancelado': color = 'grey'; status_prefix = " ❌ "
                                    elif action['is_overdue']: color = 'red'; status_prefix = f" ({action['status']}) 🔴 " # Vencido
                                    elif action['due_date_str']: color = 'orange'; status_prefix = f" ({action['status']}) 🟠 " # Pendiente con fecha
                                    
                                    st.markdown(f"<span style='color:{color};'>- {status_prefix}{disp_text}</span>", unsafe_allow_html=True)
                        else: st.caption("N/A")
                    st.subheader("📝 Resumen / Minuta")
                    st.markdown(row['summary'] if pd.notna(row['summary']) and row['summary'] else "<p><em>N/A</em></p>", unsafe_allow_html=True)
                with col_actions_disp:
                    st.markdown("##### Acciones")
                    if st.button("✏️ Editar", key=f"edit_btn_{row['meeting_id']}", use_container_width=True):
                        st.session_state.editing_meeting_id = row['meeting_id']; st.session_state.meeting_to_edit = None
                        st.session_state.active_tab = "add_edit_meeting"; st.rerun()
                    with st.popover("🗑️ Eliminar", use_container_width=True):
                        st.markdown(f"**Eliminar '{row['title']}'?**")
                        if st.button(f"Confirmar ID {row['meeting_id']}", type="primary", key=f"del_btn_{row['meeting_id']}"):
                            if delete_meeting_record(row['meeting_id']):
                                if st.session_state.editing_meeting_id == row['meeting_id']: st.session_state.editing_meeting_id = None
                                st.rerun()
                    st.download_button("📥 MD", generate_markdown_export(row), f"reunion_{row['meeting_id']}.md", "text/markdown", key=f"md_btn_{row['meeting_id']}", use_container_width=True)
                    st.download_button("📅 ICS", generate_ics_export(row), f"reunion_{row['meeting_id']}.ics", "text/calendar", key=f"ics_btn_{row['meeting_id']}", use_container_width=True)
                st.markdown("---")

with tab_actions:
    st.header("🎯 Tracker Global de Puntos de Acción")
    df_all_actions_initial = get_all_action_items()

    if df_all_actions_initial.empty:
        st.info("No hay puntos de acción registrados en ninguna reunión.")
    else:
        st.subheader("Filtros para Puntos de Acción")
        col_f1, col_f2, col_f3 = st.columns(3)
        with col_f1:
            unique_assignees = ["Todos"] + sorted(list(df_all_actions_initial['assignee'].dropna().unique()))
            filter_assignee = st.selectbox("Responsable:", unique_assignees, key="action_assign_filter")
        with col_f2:
            status_options = ["Todos"] + sorted(list(df_all_actions_initial['status'].unique()))
            filter_status = st.selectbox("Estado:", status_options, key="action_status_filter")
        with col_f3:
            search_action_task = st.text_input("Buscar en tarea:", placeholder="Palabra clave...", key="action_search_filter")

        filtered_df_actions = df_all_actions_initial.copy()
        if filter_assignee != "Todos":
            filtered_df_actions = filtered_df_actions[filtered_df_actions['assignee'] == filter_assignee]
        if filter_status != "Todos":
            filtered_df_actions = filtered_df_actions[filtered_df_actions['status'] == filter_status]
        if search_action_task:
            filtered_df_actions = filtered_df_actions[filtered_df_actions['task'].str.contains(search_action_task, case=False, na=False)]
        
        st.metric("Puntos de Acción Filtrados", len(filtered_df_actions))

        # --- Data Editor para modificar estados ---
        if not filtered_df_actions.empty:
            # Guardar el estado actual del DataFrame filtrado para comparación
            # Usamos una combinación de meeting_id y original_full_text como identificador único
            # ya que el índice del DataFrame puede cambiar con los filtros.
            
            # Crear una copia para el editor y para la comparación
            df_for_editor = filtered_df_actions.copy()
            df_for_editor['editor_id'] = df_for_editor.apply(lambda r: f"{r['meeting_id']}_{r['original_full_text']}", axis=1)

            # Almacenar en session_state ANTES de que el editor lo modifique
            # Esto solo se hace una vez por carga de datos o si los filtros cambian
            current_filter_key = f"{filter_assignee}_{filter_status}_{search_action_task}"
            if 'last_filter_key' not in st.session_state or st.session_state.last_filter_key != current_filter_key:
                st.session_state.action_items_before_edit = df_for_editor.set_index('editor_id').copy()
                st.session_state.last_filter_key = current_filter_key
            
            POSSIBLE_TASK_STATUSES = ["Pendiente", "En Progreso", "Completado", "Cancelado", "Vencido", "Abierto (sin fecha)", "Quitar Estado"]
            
            editor_column_config = {
                "task": st.column_config.TextColumn("Tarea", width="large", disabled=True),
                "assignee": st.column_config.TextColumn("Responsable", disabled=True),
                "due_date_str": st.column_config.DateColumn("Fecha Límite", format="YYYY-MM-DD", disabled=True),
                "status": st.column_config.SelectboxColumn("Estado (Editable)", options=POSSIBLE_TASK_STATUSES, required=False),
                "meeting_title": st.column_config.TextColumn("Reunión Origen", disabled=True),
                "meeting_id": None, # Ocultar
                "original_full_text": None, # Ocultar
                "text_for_reserialization": None, # Ocultar
                "is_overdue": None, # Ocultar
                "editor_id": None # Ocultar
            }
            
            st.markdown("#### Editar Estados de Tareas Directamente:")
            st.caption("Modifica la columna 'Estado (Editable)'. Los cambios se intentarán guardar al interactuar.")

            edited_df = st.data_editor(
                df_for_editor, # Pasar la copia con 'editor_id'
                column_config=editor_column_config,
                use_container_width=True,
                hide_index=True,
                key="action_items_editor"
            )

            # Comparar `edited_df` con `st.session_state.action_items_before_edit`
            if not edited_df.empty and 'action_items_before_edit' in st.session_state and not st.session_state.action_items_before_edit.empty:
                edited_df_indexed = edited_df.set_index('editor_id') # Asegurarse de que el índice sea el mismo
                
                for editor_id_val, edited_row in edited_df_indexed.iterrows():
                    if editor_id_val in st.session_state.action_items_before_edit.index:
                        original_row = st.session_state.action_items_before_edit.loc[editor_id_val]
                        if edited_row['status'] != original_row['status']:
                            # st.write(f"Cambio detectado para tarea ID {editor_id_val}: de '{original_row['status']}' a '{edited_row['status']}'") # Debug
                            update_success = update_action_item_status(
                                original_row['meeting_id'], 
                                original_row['original_full_text'], 
                                edited_row['status']
                            )
                            if update_success:
                                # Actualizar la base de comparación para evitar re-actualizaciones infinitas
                                st.session_state.action_items_before_edit.loc[editor_id_val, 'status'] = edited_row['status']
                                get_all_action_items.clear() # Forzar recarga de datos
                                st.rerun() # Refrescar la vista completa
                            else:
                                st.error(f"No se pudo actualizar el estado de la tarea '{original_row['task']}'. El cambio no se guardó.")
                                # Podrías intentar revertir el cambio en `edited_df` aquí o simplemente re-ejecutar
                                st.rerun() 
                                break # Salir del bucle si hay un error para evitar cascadas

        else: # Si filtered_df_actions está vacío después de filtrar
            st.info("No hay puntos de acción que coincidan con los filtros actuales.")


with tab_stats:
    st.header("📊 Estadísticas de Reuniones")
    df_stats = read_meeting_records(sort_by="meeting_date", ascending=True)
    if df_stats.empty: st.info("No hay datos para generar estadísticas.")
    else:
        df_stats['meeting_date'] = pd.to_datetime(df_stats['meeting_date'])
        st.subheader("📅 Reuniones por Mes")
        df_stats['month_year'] = df_stats['meeting_date'].dt.to_period('M').astype(str)
        meetings_per_month = df_stats.groupby('month_year').size().reset_index(name='count')
        chart_months = alt.Chart(meetings_per_month).mark_bar().encode(x=alt.X('month_year:O', title='Mes', sort=None), y=alt.Y('count:Q', title='Nº Reuniones'), tooltip=['month_year', 'count']).properties(title='Nº Reuniones Mensuales')
        st.altair_chart(chart_months, use_container_width=True)

        st.subheader("🏷️ Distribución por Categoría")
        df_stats['category_filled'] = df_stats['category'].fillna("Sin Categoría")
        meetings_per_category = df_stats.groupby('category_filled').size().reset_index(name='count')
        chart_categories = alt.Chart(meetings_per_category).mark_arc(innerRadius=50).encode(theta=alt.Theta(field="count", type="quantitative"), color=alt.Color(field="category_filled", type="nominal", title="Categoría"), tooltip=['category_filled', 'count']).properties(title='Distribución por Categoría')
        st.altair_chart(chart_categories, use_container_width=True)

        st.subheader("🚦 Distribución por Prioridad")
        meetings_per_priority = df_stats.groupby('priority').size().reset_index(name='count')
        chart_priority = alt.Chart(meetings_per_priority).mark_bar().encode(x=alt.X('priority:N', title='Prioridad', sort=["Alta", "Media", "Baja"]), y=alt.Y('count:Q', title='Nº Reuniones'), color=alt.Color('priority:N', scale=alt.Scale(domain=['Alta', 'Media', 'Baja'], range=['red', 'orange', 'steelblue'])), tooltip=['priority', 'count']).properties(title='Distribución por Prioridad')
        st.altair_chart(chart_priority, use_container_width=True)

# --- Barra Lateral (Sidebar) ---
st.sidebar.header("⚙️ Opciones Globales")
# ... tu botón de refrescar ...

st.sidebar.subheader("📢 Recordatorios")
google_chat_webhook = st.secrets.get("google_chat_webhook_url", "")
if not google_chat_webhook or google_chat_webhook == "TU_URL_DE_WEBHOOK_AQUÍ":
    st.sidebar.warning("Webhook de Google Chat no configurado en secrets.toml")
else:
    if st.sidebar.button("📲 Enviar Recordatorios Pendientes Ahora", use_container_width=True):
        with st.spinner("Comprobando y enviando recordatorios..."):
            check_and_send_due_task_reminders(google_chat_webhook)
        st.sidebar.success("Proceso de recordatorios finalizado.")
        st.toast("¡Revisión de recordatorios completada!")

st.sidebar.markdown("---")
st.sidebar.info("Dependencias: `streamlit-quill`, `ics`, `altair`")