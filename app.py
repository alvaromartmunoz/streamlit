import streamlit as st
import pandas as pd
from datetime import datetime, date, timedelta
from sqlalchemy import text, create_engine
import re
from streamlit_quill import st_quill
from ics import Calendar, Event
import altair as alt
import requests
import json
from typing import Dict, List, Optional, Tuple, Any
import logging
from dataclasses import dataclass, asdict
from enum import Enum
import hashlib
from functools import lru_cache
import pytz

# --- Configuración de Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuración de la Página ---
st.set_page_config(
    page_title="📋 Gestor de Notas",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        'Get Help': 'https://github.com/tu-usuario/gestor-notas',
        'Report a bug': "https://github.com/tu-usuario/gestor-notas/issues",
        'About': "# Gestor de Notas\nUna aplicación para gestionar reuniones y tareas."
    }
)

# --- Constantes y Enums ---
class Priority(Enum):
    ALTA = "Alta"
    MEDIA = "Media"
    BAJA = "Baja"

class TaskStatus(Enum):
    PENDIENTE = "Pendiente"
    EN_PROGRESO = "En Progreso"
    COMPLETADO = "Completado"
    CANCELADO = "Cancelado"
    VENCIDO = "Vencido"
    ABIERTO = "Abierto (sin fecha)"

# --- Data Classes ---
@dataclass
class ActionItem:
    task: str
    assignee: Optional[str] = None
    due_date_str: Optional[str] = None
    is_overdue: bool = False
    original_full_text: str = ""
    text_for_reserialization: str = ""
    meeting_id: Optional[int] = None
    meeting_title: Optional[str] = None
    status: str = TaskStatus.PENDIENTE.value

@dataclass
class Meeting:
    title: str
    meeting_date: datetime
    category: Optional[str] = None
    priority: str = Priority.MEDIA.value
    attendees: Optional[str] = None
    summary: Optional[str] = None
    action_items: Optional[str] = None
    meeting_id: Optional[int] = None
    created_at: Optional[datetime] = None

# --- Gestión de Conexión a Base de Datos ---
class DatabaseManager:
    def __init__(self):
        self.connection = None
        self._initialize_connection()
        self._create_tables()
    
    @st.cache_resource
    def _initialize_connection(_self):
        """Inicializa la conexión a la base de datos con reintentos."""
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                _self.connection = st.connection("mariadb_meetings_db", type="sql")
                logger.info("Conexión a base de datos establecida exitosamente")
                return _self.connection
            except Exception as e:
                logger.error(f"Intento {attempt + 1} fallido: {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    st.error(f"❌ Error al conectar con la base de datos después de {max_retries} intentos")
                    st.stop()
    
    def _create_tables(self):
        """Crea las tablas necesarias si no existen."""
        tables_sql = {
            "meetings": """
                CREATE TABLE IF NOT EXISTS meetings (
                    meeting_id INT AUTO_INCREMENT PRIMARY KEY,
                    title VARCHAR(255) NOT NULL,
                    meeting_date DATETIME NOT NULL,
                    category VARCHAR(100) NULL,
                    priority VARCHAR(20) NULL DEFAULT 'Media',
                    attendees TEXT NULL,
                    summary TEXT NULL,
                    action_items TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_meeting_date (meeting_date),
                    INDEX idx_category (category),
                    INDEX idx_priority (priority)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """,
            "action_items_history": """
                CREATE TABLE IF NOT EXISTS action_items_history (
                    history_id INT AUTO_INCREMENT PRIMARY KEY,
                    meeting_id INT NOT NULL,
                    action_item_hash VARCHAR(64) NOT NULL,
                    old_status VARCHAR(50),
                    new_status VARCHAR(50),
                    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    changed_by VARCHAR(100),
                    FOREIGN KEY (meeting_id) REFERENCES meetings(meeting_id) ON DELETE CASCADE,
                    INDEX idx_meeting_action (meeting_id, action_item_hash)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """,
            "reminders_sent": """
                CREATE TABLE IF NOT EXISTS reminders_sent (
                    reminder_id INT AUTO_INCREMENT PRIMARY KEY,
                    meeting_id INT NOT NULL,
                    action_item_hash VARCHAR(64) NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reminder_type VARCHAR(50),
                    FOREIGN KEY (meeting_id) REFERENCES meetings(meeting_id) ON DELETE CASCADE,
                    UNIQUE KEY unique_reminder (meeting_id, action_item_hash, reminder_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
        }
        
        try:
            with self.connection.session as session:
                for table_name, create_sql in tables_sql.items():
                    session.execute(text(create_sql))
                session.commit()
                logger.info("Tablas creadas/verificadas exitosamente")
        except Exception as e:
            logger.error(f"Error al crear tablas: {str(e)}")
            st.error(f"❌ Error al crear/verificar tablas en la base de datos")

# --- Inicializar gestor de base de datos ---
db_manager = DatabaseManager()
conn = db_manager.connection

# --- Funciones de Utilidad ---
def generate_action_item_hash(action_item_text: str, meeting_id: int) -> str:
    """Genera un hash único para un action item."""
    content = f"{meeting_id}:{action_item_text}"
    return hashlib.sha256(content.encode()).hexdigest()

def validate_date_format(date_str: str) -> bool:
    """Valida el formato de fecha YYYY-MM-DD."""
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False

def sanitize_html(html_content: str) -> str:
    """Limpia y valida contenido HTML."""
    # Implementación básica - considera usar bleach para producción
    if not html_content:
        return ""
    # Remover scripts y estilos peligrosos
    html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    html_content = re.sub(r'on\w+\s*=\s*["\'][^"\']*["\']', '', html_content)
    return html_content

# --- Parser de Action Items Mejorado ---
class ActionItemParser:
    @staticmethod
    def parse(item_text_line: str, meeting_id: Optional[int] = None, 
              meeting_title: Optional[str] = None) -> ActionItem:
        """Parser mejorado para action items con mejor manejo de errores."""
        original_full_text = item_text_line.strip()
        
        # Detectar estado en corchetes
        status_match = re.match(r"\[(.*?)\]\s*(.*)", original_full_text)
        current_status_from_prefix = None
        text_for_parsing_parts = original_full_text
        
        if status_match:
            current_status_from_prefix = status_match.group(1).strip()
            text_for_parsing_parts = status_match.group(2).strip()
        
        # Dividir por separador
        parts = [p.strip() for p in text_for_parsing_parts.split(" - ", 2)]
        
        action_item = ActionItem(
            task=parts[0],
            original_full_text=original_full_text,
            text_for_reserialization=text_for_parsing_parts,
            meeting_id=meeting_id,
            meeting_title=meeting_title
        )
        
        # Procesar partes adicionales
        if len(parts) >= 2:
            action_item = ActionItemParser._process_additional_parts(action_item, parts[1:])
        
        # Determinar estado final
        action_item = ActionItemParser._determine_final_status(
            action_item, current_status_from_prefix
        )
        
        return action_item
    
    @staticmethod
    def _process_additional_parts(action_item: ActionItem, parts: List[str]) -> ActionItem:
        """Procesa asignado y fecha de las partes adicionales."""
        for part in parts:
            if part.startswith("@"):
                action_item.assignee = part[1:]
            elif validate_date_format(part):
                action_item.due_date_str = part
                # Verificar si está vencida
                due_date = datetime.strptime(part, "%Y-%m-%d").date()
                if due_date < date.today():
                    action_item.is_overdue = True
            else:
                # Si no es asignado ni fecha, podría ser parte del asignado
                if action_item.assignee and not action_item.due_date_str:
                    action_item.assignee += f" {part}"
        
        return action_item
    
    @staticmethod
    def _determine_final_status(action_item: ActionItem, 
                               status_from_prefix: Optional[str]) -> ActionItem:
        """Determina el estado final del action item."""
        if status_from_prefix:
            action_item.status = status_from_prefix
            if status_from_prefix in [TaskStatus.COMPLETADO.value, TaskStatus.CANCELADO.value]:
                action_item.is_overdue = False
        elif action_item.is_overdue:
            action_item.status = TaskStatus.VENCIDO.value
        elif action_item.due_date_str:
            action_item.status = TaskStatus.PENDIENTE.value
        else:
            action_item.status = TaskStatus.ABIERTO.value
        
        return action_item

# --- Funciones CRUD Mejoradas ---
class MeetingRepository:
    @staticmethod
    def create(meeting: Meeting) -> bool:
        """Crea un nuevo registro de reunión."""
        sql = text("""
            INSERT INTO meetings (title, meeting_date, category, priority, 
                                attendees, summary, action_items)
            VALUES (:title, :date, :category, :priority, :attendees, 
                    :summary, :actions)
        """)
        
        try:
            with conn.session as session:
                session.execute(sql, {
                    "title": meeting.title,
                    "date": meeting.meeting_date,
                    "category": meeting.category,
                    "priority": meeting.priority,
                    "attendees": meeting.attendees,
                    "summary": sanitize_html(meeting.summary),
                    "actions": meeting.action_items
                })
                session.commit()
            
            # Limpiar caché
            MeetingRepository.clear_cache()
            st.success("✅ Registro de reunión guardado exitosamente")
            return True
            
        except Exception as e:
            logger.error(f"Error al crear reunión: {str(e)}")
            st.error(f"❌ Error al guardar el registro: {str(e)}")
            return False
    
    @staticmethod
    @st.cache_data(ttl=60)
    def read(filters: Dict[str, Any]) -> pd.DataFrame:
        """Lee registros de reuniones con filtros."""
        where_clauses = []
        params = {}
        
        # Construir cláusulas WHERE dinámicamente
        if filters.get("search_term"):
            search_like = f"%{filters['search_term']}%"
            where_clauses.append(
                "(title LIKE :search_term OR attendees LIKE :search_term "
                "OR summary LIKE :search_term OR action_items LIKE :search_term "
                "OR category LIKE :search_term)"
            )
            params["search_term"] = search_like
        
        if filters.get("date_from"):
            where_clauses.append("meeting_date >= :date_from")
            params["date_from"] = datetime.combine(
                filters["date_from"], datetime.min.time()
            )
        
        if filters.get("date_to"):
            where_clauses.append("meeting_date <= :date_to")
            params["date_to"] = datetime.combine(
                filters["date_to"], datetime.max.time()
            )
        
        if filters.get("category") and filters["category"] != "Todas":
            where_clauses.append("category = :category")
            params["category"] = filters["category"]
        
        if filters.get("priority") and filters["priority"] != "Todas":
            where_clauses.append("priority = :priority")
            params["priority"] = filters["priority"]
        
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        
        order_direction = "ASC" if filters.get("ascending", False) else "DESC"
        sort_column = filters.get("sort_by", "meeting_date")
        
        # Validar columna de ordenamiento
        valid_columns = ["meeting_date", "title", "category", "priority", "created_at"]
        if sort_column not in valid_columns:
            sort_column = "meeting_date"
        
        query = f"""
            SELECT meeting_id, title, meeting_date, category, priority, 
                   attendees, summary, action_items, created_at, updated_at
            FROM meetings
            {where_sql}
            ORDER BY {sort_column} {order_direction}
        """
        
        try:
            df = conn.query(query, params=params)
            return df
        except Exception as e:
            logger.error(f"Error al leer registros: {str(e)}")
            st.error(f"❌ Error al leer registros: {str(e)}")
            return pd.DataFrame()
    
    @staticmethod
    def update(meeting: Meeting) -> bool:
        """Actualiza un registro de reunión existente."""
        if not meeting.meeting_id:
            st.error("❌ ID de reunión no válido")
            return False
        
        sql = text("""
            UPDATE meetings
            SET title = :title, meeting_date = :date, category = :category,
                priority = :priority, attendees = :attendees, summary = :summary,
                action_items = :actions
            WHERE meeting_id = :meeting_id
        """)
        
        try:
            with conn.session as session:
                result = session.execute(sql, {
                    "title": meeting.title,
                    "date": meeting.meeting_date,
                    "category": meeting.category,
                    "priority": meeting.priority,
                    "attendees": meeting.attendees,
                    "summary": sanitize_html(meeting.summary),
                    "actions": meeting.action_items,
                    "meeting_id": meeting.meeting_id
                })
                session.commit()
                
                if result.rowcount == 0:
                    st.warning("⚠️ No se encontró la reunión para actualizar")
                    return False
            
            MeetingRepository.clear_cache()
            st.success(f"✅ Registro ID {meeting.meeting_id} actualizado")
            return True
            
        except Exception as e:
            logger.error(f"Error al actualizar reunión: {str(e)}")
            st.error(f"❌ Error al actualizar registro: {str(e)}")
            return False
    
    @staticmethod
    def delete(meeting_id: int) -> bool:
        """Elimina un registro de reunión."""
        sql = text("DELETE FROM meetings WHERE meeting_id = :meeting_id")
        
        try:
            with conn.session as session:
                result = session.execute(sql, {"meeting_id": meeting_id})
                session.commit()
                
                if result.rowcount == 0:
                    st.warning("⚠️ No se encontró la reunión para eliminar")
                    return False
            
            MeetingRepository.clear_cache()
            st.success(f"🗑️ Registro ID {meeting_id} eliminado")
            return True
            
        except Exception as e:
            logger.error(f"Error al eliminar reunión: {str(e)}")
            st.error(f"❌ Error al eliminar registro: {str(e)}")
            return False
    
    @staticmethod
    def clear_cache():
        """Limpia el caché de las funciones relacionadas."""
        MeetingRepository.read.clear()
        get_distinct_categories.clear()
        get_all_action_items.clear()

# --- Funciones de Action Items ---
class ActionItemService:
    @staticmethod
    def update_status(meeting_id: int, original_text: str, new_status: str) -> bool:
        """Actualiza el estado de un action item con historial."""
        try:
            # Obtener action items actuales
            meeting_df = conn.query(
                "SELECT action_items FROM meetings WHERE meeting_id = :id",
                params={"id": meeting_id},
                ttl=0
            )
            
            if meeting_df.empty:
                st.error(f"No se encontró la reunión con ID {meeting_id}")
                return False
            
            current_items = meeting_df.iloc[0]['action_items'] or ""
            items_list = current_items.split('\n')
            new_items_list = []
            found = False
            
            # Generar hash del item original
            item_hash = generate_action_item_hash(original_text, meeting_id)
            
            for item_line in items_list:
                if item_line.strip() == original_text.strip():
                    parsed = ActionItemParser.parse(original_text)
                    
                    # Guardar historial del cambio
                    ActionItemService._save_status_history(
                        meeting_id, item_hash, parsed.status, new_status
                    )
                    
                    # Actualizar línea
                    if new_status == "Quitar Estado" or not new_status:
                        new_items_list.append(parsed.text_for_reserialization)
                    else:
                        new_items_list.append(f"[{new_status}] {parsed.text_for_reserialization}")
                    found = True
                else:
                    new_items_list.append(item_line)
            
            if not found:
                st.warning("No se encontró la tarea exacta para actualizar")
                return False
            
            # Actualizar en base de datos
            updated_items = "\n".join(new_items_list)
            sql = text("UPDATE meetings SET action_items = :actions WHERE meeting_id = :id")
            
            with conn.session as session:
                session.execute(sql, {"actions": updated_items, "id": meeting_id})
                session.commit()
            
            MeetingRepository.clear_cache()
            st.toast(f"✅ Estado actualizado a '{new_status}'", icon="🎉")
            return True
            
        except Exception as e:
            logger.error(f"Error al actualizar estado: {str(e)}")
            st.error(f"❌ Error al actualizar el estado: {str(e)}")
            return False
    
    @staticmethod
    def _save_status_history(meeting_id: int, item_hash: str, 
                           old_status: str, new_status: str):
        """Guarda el historial de cambios de estado."""
        sql = text("""
            INSERT INTO action_items_history 
            (meeting_id, action_item_hash, old_status, new_status, changed_by)
            VALUES (:meeting_id, :item_hash, :old_status, :new_status, :changed_by)
        """)
        
        try:
            with conn.session as session:
                session.execute(sql, {
                    "meeting_id": meeting_id,
                    "item_hash": item_hash,
                    "old_status": old_status,
                    "new_status": new_status,
                    "changed_by": st.session_state.get("user_email", "Sistema")
                })
                session.commit()
        except Exception as e:
            logger.warning(f"No se pudo guardar historial: {str(e)}")

# --- Sistema de Notificaciones Mejorado ---
class NotificationService:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.session = requests.Session()
        self.session.headers.update({'Content-Type': 'application/json; charset=UTF-8'})
    
    def send_task_reminder(self, task_details: Dict[str, Any]) -> bool:
        """Envía recordatorio de tarea a Google Chat."""
        if not self._validate_webhook():
            return False
        
        message = self._build_reminder_message(task_details)
        
        try:
            response = self.session.post(
                self.webhook_url,
                data=json.dumps(message),
                timeout=10
            )
            response.raise_for_status()
            
            # Registrar envío exitoso
            self._record_reminder_sent(task_details)
            logger.info(f"Recordatorio enviado para: {task_details.get('task')}")
            return True
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error al enviar notificación: {str(e)}")
            return False
    
    def _validate_webhook(self) -> bool:
        """Valida que el webhook esté configurado correctamente."""
        if not self.webhook_url or self.webhook_url == "TU_URL_DE_WEBHOOK_AQUÍ":
            logger.warning("Webhook de Google Chat no configurado")
            return False
        return True
    
    def _build_reminder_message(self, task_details: Dict[str, Any]) -> Dict:
        """Construye el mensaje para Google Chat."""
        title, emoji = self._get_title_and_emoji(task_details)
        
        return {
            "cardsV2": [{
                "cardId": f"reminder_{task_details.get('meeting_id')}_{int(datetime.now().timestamp())}",
                "card": {
                    "header": {
                        "title": title,
                        "subtitle": f"Reunión: {task_details.get('meeting_title', 'N/A')}",
                        "imageUrl": "https://cdn-icons-png.flaticon.com/512/10828/10828783.png",
                        "imageType": "CIRCLE"
                    },
                    "sections": [{
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
                                    "text": task_details.get('due_date_str', 'Sin fecha')
                                }
                            },
                            {
                                "decoratedText": {
                                    "topLabel": "ASIGNADO A",
                                    "text": task_details.get('assignee', 'Sin asignar')
                                }
                            },
                            {
                                "decoratedText": {
                                    "topLabel": "ESTADO",
                                    "text": f"{emoji} {task_details.get('status', 'N/A')}"
                                }
                            }
                        ]
                    }]
                }
            }]
        }
    
    def _get_title_and_emoji(self, task_details: Dict[str, Any]) -> Tuple[str, str]:
        """Determina el título y emoji según el estado de la tarea."""
        if task_details.get('is_overdue'):
            return "⚠️ Tarea Vencida ⚠️", "🔴"
        elif task_details.get('due_date_str') == date.today().strftime('%Y-%m-%d'):
            return "📢 Tarea para Hoy 📢", "🟡"
        elif task_details.get('due_date_str') == (date.today() + timedelta(days=1)).strftime('%Y-%m-%d'):
            return "💡 Tarea para Mañana 💡", "🟠"
        else:
            return "🔔 Recordatorio de Tarea 🔔", "➡️"
    
    def _record_reminder_sent(self, task_details: Dict[str, Any]):
        """Registra que se envió un recordatorio."""
        if not task_details.get('meeting_id'):
            return
        
        item_hash = generate_action_item_hash(
            task_details.get('original_full_text', ''),
            task_details['meeting_id']
        )
        
        sql = text("""
            INSERT IGNORE INTO reminders_sent 
            (meeting_id, action_item_hash, reminder_type)
            VALUES (:meeting_id, :item_hash, :reminder_type)
        """)
        
        try:
            with conn.session as session:
                session.execute(sql, {
                    "meeting_id": task_details['meeting_id'],
                    "item_hash": item_hash,
                    "reminder_type": self._get_reminder_type(task_details)
                })
                session.commit()
        except Exception as e:
            logger.warning(f"No se pudo registrar recordatorio enviado: {str(e)}")
    
    def _get_reminder_type(self, task_details: Dict[str, Any]) -> str:
        """Determina el tipo de recordatorio."""
        if task_details.get('is_overdue'):
            return "overdue"
        elif task_details.get('due_date_str') == date.today().strftime('%Y-%m-%d'):
            return "today"
        elif task_details.get('due_date_str') == (date.today() + timedelta(days=1)).strftime('%Y-%m-%d'):
            return "tomorrow"
        else:
            return "upcoming"
    
    def check_and_send_reminders(self):
        """Verifica y envía recordatorios pendientes."""
        logger.info("Iniciando verificación de recordatorios...")
        
        all_tasks = get_all_action_items()
        if all_tasks.empty:
            logger.info("No hay tareas para verificar")
            return
        
        sent_count = 0
        today = date.today()
        tomorrow = today + timedelta(days=1)
        
        for _, task in all_tasks.iterrows():
            if self._should_send_reminder(task, today, tomorrow):
                task_details = self._prepare_task_details(task)
                if self.send_task_reminder(task_details):
                    sent_count += 1
        
        logger.info(f"Proceso finalizado. {sent_count} recordatorios enviados")
        st.success(f"✅ {sent_count} recordatorios enviados")
    
    def _should_send_reminder(self, task: pd.Series, today: date, tomorrow: date) -> bool:
        """Determina si se debe enviar recordatorio para una tarea."""
        # No enviar para tareas completadas o canceladas
        if task.get('status', '').lower() in ['completado', 'cancelado']:
            return False
        
        due_date_str = task.get('due_date_str')
        if not due_date_str:
            return False
        
        try:
            due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
            
            # Verificar si ya se envió recordatorio hoy
            if self._already_sent_today(task):
                return False
            
            # Enviar si vence hoy, mañana o está vencida (máx 7 días)
            if due_date == today or due_date == tomorrow:
                return True
            elif due_date < today:
                days_overdue = (today - due_date).days
                return days_overdue <= 7 and days_overdue % 3 == 0
            
            return False
            
        except ValueError:
            return False
    
    def _already_sent_today(self, task: pd.Series) -> bool:
        """Verifica si ya se envió recordatorio hoy para esta tarea."""
        if not task.get('meeting_id'):
            return False
        
        item_hash = generate_action_item_hash(
            task.get('original_full_text', ''),
            task['meeting_id']
        )
        
        sql = text("""
            SELECT COUNT(*) as count
            FROM reminders_sent
            WHERE meeting_id = :meeting_id 
            AND action_item_hash = :item_hash
            AND DATE(sent_at) = CURDATE()
        """)
        
        try:
            result = conn.query(sql, params={
                "meeting_id": task['meeting_id'],
                "item_hash": item_hash
            })
            return result.iloc[0]['count'] > 0
        except Exception:
            return False
    
    def _prepare_task_details(self, task: pd.Series) -> Dict[str, Any]:
        """Prepara los detalles de la tarea para el recordatorio."""
        return {
            'task': task.get('task'),
            'due_date_str': task.get('due_date_str'),
            'meeting_title': task.get('meeting_title'),
            'assignee': task.get('assignee'),
            'status': task.get('status'),
            'meeting_id': task.get('meeting_id'),
            'is_overdue': task.get('is_overdue', False),
            'original_full_text': task.get('original_full_text', '')
        }

# --- Funciones de Exportación ---
class ExportService:
    @staticmethod
    def to_markdown(meeting_data: pd.Series) -> str:
        """Exporta reunión a formato Markdown."""
        md = f"# {meeting_data['title']}\n\n"
        md += f"**ID:** `{meeting_data['meeting_id']}`\n"
        md += f"**Fecha:** {pd.to_datetime(meeting_data['meeting_date']).strftime('%Y-%m-%d %H:%M')}\n"
        
        if pd.notna(meeting_data.get('category')):
            md += f"**Categoría:** {meeting_data['category']}\n"
        
        md += f"**Prioridad:** {meeting_data.get('priority', 'N/A')}\n"
        md += f"**Creado:** {pd.to_datetime(meeting_data['created_at']).strftime('%Y-%m-%d %H:%M')}\n"
        
        if pd.notna(meeting_data.get('updated_at')):
            md += f"**Actualizado:** {pd.to_datetime(meeting_data['updated_at']).strftime('%Y-%m-%d %H:%M')}\n"
        
        md += "\n## 👥 Asistentes\n"
        if pd.notna(meeting_data.get('attendees')) and meeting_data['attendees']:
            for att in meeting_data['attendees'].split('\n'):
                if att.strip():
                    md += f"- {att.strip()}\n"
        else:
            md += "_Sin asistentes registrados_\n"
        
        md += "\n## 📝 Resumen\n"
        if pd.notna(meeting_data.get('summary')) and meeting_data['summary']:
            clean_summary = re.sub('<[^<]+?>', '', meeting_data['summary'])
            md += f"{clean_summary}\n"
        else:
            md += "_Sin resumen_\n"
        
        md += "\n## 📌 Puntos de Acción\n"
        if pd.notna(meeting_data.get('action_items')) and meeting_data['action_items']:
            for item_text in meeting_data['action_items'].split('\n'):
                if item_text.strip():
                    action = ActionItemParser.parse(item_text.strip())
                    md += f"- **[{action.status}]** {action.task}"
                    
                    if action.assignee:
                        md += f" - @{action.assignee}"
                    if action.due_date_str:
                        md += f" - {action.due_date_str}"
                        if action.is_overdue and action.status != TaskStatus.COMPLETADO.value:
                            md += " ⚠️"
                    md += "\n"
        else:
            md += "_Sin puntos de acción_\n"
        
        return md
    
    @staticmethod
    def to_ics(meeting_data: pd.Series) -> str:
        """Exporta reunión a formato ICS (calendario)."""
        cal = Calendar()
        event = Event()
        
        event.name = meeting_data['title']
        meeting_dt = pd.to_datetime(meeting_data['meeting_date'])
        
        # Usar timezone si está disponible
        if hasattr(st.session_state, 'timezone'):
            tz = pytz.timezone(st.session_state.timezone)
            meeting_dt = tz.localize(meeting_dt)
        
        event.begin = meeting_dt
        event.end = meeting_dt + timedelta(hours=1)
        
        # Descripción
        description_parts = []
        if pd.notna(meeting_data.get('category')):
            description_parts.append(f"Categoría: {meeting_data['category']}")
        description_parts.append(f"Prioridad: {meeting_data.get('priority', 'N/A')}")
        
        if pd.notna(meeting_data.get('summary')):
            clean_summary = re.sub('<[^<]+?>', '', meeting_data['summary'])
            description_parts.append(f"\nResumen:\n{clean_summary[:500]}...")
        
        event.description = "\n".join(description_parts)
        
        # Asistentes
        if pd.notna(meeting_data.get('attendees')) and meeting_data['attendees']:
            attendees = [att.strip() for att in meeting_data['attendees'].split('\n') if att.strip()]
            event.attendees = attendees
        
        cal.events.add(event)
        return str(cal)

# --- Funciones de Caché ---
@st.cache_data(ttl=300)
def get_distinct_categories() -> List[str]:
    """Obtiene las categorías únicas de las reuniones."""
    try:
        df = conn.query("""
            SELECT DISTINCT category 
            FROM meetings 
            WHERE category IS NOT NULL AND category != '' 
            ORDER BY category ASC
        """)
        return ["Todas"] + list(df['category'].unique())
    except Exception as e:
        logger.error(f"Error al obtener categorías: {str(e)}")
        return ["Todas"]

@st.cache_data(ttl=60)
def get_all_action_items() -> pd.DataFrame:
    """Obtiene todos los action items de todas las reuniones."""
    filters = {"sort_by": "meeting_date", "ascending": False}
    all_meetings = MeetingRepository.read(filters)
    
    action_items_list = []
    
    if not all_meetings.empty:
        for _, meeting in all_meetings.iterrows():
            if pd.notna(meeting['action_items']) and meeting['action_items']:
                for item_text in meeting['action_items'].split('\n'):
                    if item_text.strip():
                        parsed = ActionItemParser.parse(
                            item_text.strip(),
                            meeting['meeting_id'],
                            meeting['title']
                        )
                        action_items_list.append(asdict(parsed))
    
    return pd.DataFrame(action_items_list)

# --- Componentes UI ---
def render_meeting_form(meeting_data: Optional[Dict] = None):
    """Renderiza el formulario de reunión."""
    is_editing = meeting_data is not None
    form_title = f"✏️ Editando Reunión ID: {meeting_data.get('meeting_id')}" if is_editing else "✍️ Registrar Nueva Reunión"
    
    st.subheader(form_title)
    
    with st.form(key=f"meeting_form_{'edit' if is_editing else 'new'}"):
        col1, col2, col3 = st.columns(3)
        
        with col1:
            title = st.text_input(
                "Título*",
                value=meeting_data.get("title", "") if meeting_data else "",
                help="Título descriptivo de la reunión"
            )
            
            meeting_date = st.date_input(
                "Fecha*",
                value=pd.to_datetime(meeting_data["meeting_date"]).date() if meeting_data and meeting_data.get("meeting_date") else date.today(),
                help="Fecha de la reunión"
            )
        
        with col2:
            category = st.text_input(
                "Categoría",
                value=meeting_data.get("category", "") if meeting_data else "",
                placeholder="Ej: Proyecto Alpha",
                help="Categoría o proyecto relacionado"
            )
            
            meeting_time = st.time_input(
                "Hora*",
                value=pd.to_datetime(meeting_data["meeting_date"]).time() if meeting_data and meeting_data.get("meeting_date") else datetime.now().time(),
                help="Hora de inicio de la reunión"
            )
        
        with col3:
            priority_options = [p.value for p in Priority]
            current_priority = meeting_data.get("priority", Priority.MEDIA.value) if meeting_data else Priority.MEDIA.value
            
            priority = st.selectbox(
                "Prioridad*",
                options=priority_options,
                index=priority_options.index(current_priority),
                help="Nivel de prioridad de la reunión"
            )
        
        attendees = st.text_area(
            "Asistentes",
            value=meeting_data.get("attendees", "") if meeting_data else "",
            placeholder="Uno por línea\nEj:\nJuan Pérez\nMaría García",
            height=100,
            help="Lista de asistentes, uno por línea"
        )
        
        st.markdown("**Resumen / Minuta:**")
        summary_html = st_quill(
            value=meeting_data.get("summary", "") if meeting_data else "",
            placeholder="Escribe los puntos clave de la reunión...",
            html=True,
            key="quill_summary"
        )
        
        action_items = st.text_area(
            "Puntos de Acción",
            value=meeting_data.get("action_items", "") if meeting_data else "",
            height=150,
            placeholder="Formato: [Estado] Tarea - @Responsable - YYYY-MM-DD\n\nEjemplos:\n[Pendiente] Revisar informe - @Ana - 2024-12-25\nEnviar propuesta - @Carlos - 2024-12-20\n[Completado] Actualizar presentación - @Luis",
            help="Un punto de acción por línea. El estado es opcional."
        )
        
        col_submit, col_cancel = st.columns([3, 1])
        
        with col_submit:
            submitted = st.form_submit_button(
                "💾 Guardar Cambios" if is_editing else "➕ Crear Reunión",
                use_container_width=True,
                type="primary"
            )
        
        if submitted:
            # Validaciones
            if not title or not meeting_date or not meeting_time:
                st.error("⚠️ Título, Fecha y Hora son obligatorios")
                return False
            
            if not summary_html or summary_html == "<p><br></p>":
                st.error("⚠️ El resumen no puede estar vacío")
                return False
            
            # Crear objeto Meeting
            meeting = Meeting(
                title=title,
                meeting_date=datetime.combine(meeting_date, meeting_time),
                category=category.strip() if category else None,
                priority=priority,
                attendees=attendees,
                summary=summary_html,
                action_items=action_items,
                meeting_id=meeting_data.get("meeting_id") if meeting_data else None
            )
            
            # Guardar o actualizar
            if is_editing:
                success = MeetingRepository.update(meeting)
            else:
                success = MeetingRepository.create(meeting)
            
            if success:
                st.session_state.editing_meeting_id = None
                st.session_state.meeting_to_edit = None
                st.rerun()
            
            return success
    
    # Botón cancelar fuera del formulario
    if is_editing:
        with col_cancel:
            if st.button("✖️ Cancelar", use_container_width=True):
                st.session_state.editing_meeting_id = None
                st.session_state.meeting_to_edit = None
                st.rerun()

def render_meeting_card(meeting: pd.Series):
    """Renderiza una tarjeta de reunión."""
    priority_config = {
        Priority.ALTA.value: {"emoji": "🔥", "color": "red"},
        Priority.MEDIA.value: {"emoji": "🔸", "color": "orange"},
        Priority.BAJA.value: {"emoji": "🔹", "color": "blue"}
    }
    
    config = priority_config.get(meeting['priority'], {"emoji": "", "color": "gray"})
    
    # Título con prioridad y fecha
    title_parts = [
        f"{config['emoji']} **{meeting['title']}**",
        f"({pd.to_datetime(meeting['meeting_date']).strftime('%d %b %Y, %H:%M')})"
    ]
    
    if pd.notna(meeting.get('category')) and meeting['category']:
        title_parts.append(f"| 📁 _{meeting['category']}_")
    
    with st.expander(" ".join(title_parts)):
        # Metadatos
        col_meta1, col_meta2 = st.columns(2)
        with col_meta1:
            st.caption(f"**ID:** `{meeting['meeting_id']}`")
        with col_meta2:
            st.caption(f"**Creado:** {pd.to_datetime(meeting['created_at']).strftime('%Y-%m-%d %H:%M')}")
        
        # Contenido principal
        col_content, col_actions = st.columns([0.75, 0.25])
        
        with col_content:
            # Asistentes y Action Items en columnas
            col_att, col_act = st.columns(2)
            
            with col_att:
                st.markdown("#### 👥 Asistentes")
                if pd.notna(meeting['attendees']) and meeting['attendees']:
                    attendees = [a.strip() for a in meeting['attendees'].split('\n') if a.strip()]
                    for att in attendees[:5]:  # Mostrar máx 5
                        st.markdown(f"- {att}")
                    if len(attendees) > 5:
                        st.caption(f"... y {len(attendees) - 5} más")
                else:
                    st.caption("_Sin asistentes registrados_")
            
            with col_act:
                st.markdown("#### 📌 Puntos de Acción")
                if pd.notna(meeting['action_items']) and meeting['action_items']:
                    action_count = 0
                    for item_text in meeting['action_items'].split('\n'):
                        if item_text.strip() and action_count < 3:  # Mostrar máx 3
                            action = ActionItemParser.parse(item_text.strip())
                            
                            # Determinar estilo según estado
                            if action.status == TaskStatus.COMPLETADO.value:
                                st.markdown(f"✅ ~~{action.task}~~")
                            elif action.status == TaskStatus.VENCIDO.value:
                                st.markdown(f"🔴 **{action.task}**")
                            elif action.status == TaskStatus.EN_PROGRESO.value:
                                st.markdown(f"🟡 _{action.task}_")
                            else:
                                st.markdown(f"⚪ {action.task}")
                            
                            action_count += 1
                    
                    total_actions = len([i for i in meeting['action_items'].split('\n') if i.strip()])
                    if total_actions > 3:
                        st.caption(f"... y {total_actions - 3} más")
                else:
                    st.caption("_Sin puntos de acción_")
            
            # Resumen
            st.markdown("#### 📝 Resumen")
            if pd.notna(meeting['summary']) and meeting['summary']:
                # Mostrar solo primeras líneas del resumen
                clean_summary = re.sub('<[^<]+?>', '', meeting['summary'])
                summary_preview = clean_summary[:200] + "..." if len(clean_summary) > 200 else clean_summary
                st.markdown(summary_preview)
            else:
                st.caption("_Sin resumen_")
        
        with col_actions:
            st.markdown("##### ⚡ Acciones")
            
            # Botón Editar
            if st.button("✏️ Editar", key=f"edit_{meeting['meeting_id']}", use_container_width=True):
                st.session_state.editing_meeting_id = meeting['meeting_id']
                st.session_state.meeting_to_edit = None
                st.rerun()
            
            # Botón Eliminar con confirmación
            with st.popover("🗑️ Eliminar", use_container_width=True):
                st.warning(f"¿Eliminar **{meeting['title']}**?")
                st.caption("Esta acción no se puede deshacer")
                
                if st.button(
                    "Confirmar eliminación",
                    key=f"confirm_del_{meeting['meeting_id']}",
                    type="primary",
                    use_container_width=True
                ):
                    if MeetingRepository.delete(meeting['meeting_id']):
                        st.rerun()
            
            # Exportaciones
            st.download_button(
                "📄 Markdown",
                ExportService.to_markdown(meeting),
                f"reunion_{meeting['meeting_id']}_{meeting['title'][:20]}.md",
                "text/markdown",
                key=f"md_{meeting['meeting_id']}",
                use_container_width=True
            )
            
            st.download_button(
                "📅 Calendario",
                ExportService.to_ics(meeting),
                f"reunion_{meeting['meeting_id']}_{meeting['title'][:20]}.ics",
                "text/calendar",
                key=f"ics_{meeting['meeting_id']}",
                use_container_width=True
            )

def render_statistics(df_meetings: pd.DataFrame):
    """Renderiza las estadísticas de reuniones."""
    if df_meetings.empty:
        st.info("📊 No hay datos suficientes para generar estadísticas")
        return
    
    # Preparar datos
    df_stats = df_meetings.copy()
    df_stats['meeting_date'] = pd.to_datetime(df_stats['meeting_date'])
    
    # Métricas generales
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("Total Reuniones", len(df_stats))
    
    with col2:
        total_actions = sum(
            len([i for i in str(items).split('\n') if i.strip()])
            for items in df_stats['action_items'] if pd.notna(items)
        )
        st.metric("Total Puntos de Acción", total_actions)
    
    with col3:
        avg_actions = total_actions / len(df_stats) if len(df_stats) > 0 else 0
        st.metric("Promedio Acciones/Reunión", f"{avg_actions:.1f}")
    
    with col4:
        categories_count = df_stats['category'].nunique()
        st.metric("Categorías Únicas", categories_count)
    
    st.markdown("---")
    
    # Gráficos
    tab1, tab2, tab3, tab4 = st.tabs([
        "📅 Temporal", "🏷️ Categorías", "🚦 Prioridades", "👥 Participación"
    ])
    
    with tab1:
        # Reuniones por mes
        df_stats['month_year'] = df_stats['meeting_date'].dt.to_period('M').astype(str)
        meetings_per_month = df_stats.groupby('month_year').size().reset_index(name='count')
        
        chart_timeline = alt.Chart(meetings_per_month).mark_line(point=True).encode(
            x=alt.X('month_year:O', title='Mes', sort=None),
            y=alt.Y('count:Q', title='Número de Reuniones'),
            tooltip=['month_year', 'count']
        ).properties(
            title='Evolución Mensual de Reuniones',
            height=400
        ).interactive()
        
        st.altair_chart(chart_timeline, use_container_width=True)
        
        # Heatmap de días de la semana
        df_stats['weekday'] = df_stats['meeting_date'].dt.day_name()
        df_stats['hour'] = df_stats['meeting_date'].dt.hour
        
        heatmap_data = df_stats.groupby(['weekday', 'hour']).size().reset_index(name='count')
        
        # Ordenar días de la semana
        weekday_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        heatmap_data['weekday'] = pd.Categorical(heatmap_data['weekday'], categories=weekday_order, ordered=True)
        
        heatmap = alt.Chart(heatmap_data).mark_rect().encode(
            x=alt.X('hour:O', title='Hora del día'),
            y=alt.Y('weekday:O', title='Día de la semana', sort=weekday_order),
            color=alt.Color('count:Q', title='Reuniones', scale=alt.Scale(scheme='blues')),
            tooltip=['weekday', 'hour', 'count']
        ).properties(
            title='Distribución de Reuniones por Día y Hora',
            height=300
        )
        
        st.altair_chart(heatmap, use_container_width=True)
    
    with tab2:
        # Distribución por categoría
        df_stats['category_filled'] = df_stats['category'].fillna("Sin Categoría")
        category_stats = df_stats.groupby('category_filled').agg({
            'meeting_id': 'count',
            'action_items': lambda x: sum(len([i for i in str(items).split('\n') if i.strip()]) for items in x if pd.notna(items))
        }).reset_index()
        category_stats.columns = ['category', 'meetings', 'actions']
        
        # Gráfico de barras para reuniones por categoría
        bar_chart = alt.Chart(category_stats).mark_bar().encode(
            x=alt.X('meetings:Q', title='Número de Reuniones'),
            y=alt.Y('category:N', title='Categoría', sort='-x'),
            color=alt.Color('actions:Q', title='Total Acciones', scale=alt.Scale(scheme='viridis')),
            tooltip=['category', 'meetings', 'actions']
        ).properties(
            title='Reuniones y Acciones por Categoría',
            height=400
        )
        
        st.altair_chart(bar_chart, use_container_width=True)
        
        # Top categorías
        st.subheader("🏆 Top 5 Categorías más Activas")
        top_categories = category_stats.nlargest(5, 'meetings')[['category', 'meetings', 'actions']]
        st.dataframe(top_categories, use_container_width=True, hide_index=True)
    
    with tab3:
        # Distribución por prioridad
        priority_stats = df_stats.groupby('priority').size().reset_index(name='count')
        
        # Gráfico de dona
        donut_chart = alt.Chart(priority_stats).mark_arc(innerRadius=50).encode(
            theta=alt.Theta(field="count", type="quantitative"),
            color=alt.Color(
                field="priority",
                type="nominal",
                scale=alt.Scale(
                    domain=[Priority.ALTA.value, Priority.MEDIA.value, Priority.BAJA.value],
                    range=['#ff4444', '#ffaa00', '#4444ff']
                ),
                title="Prioridad"
            ),
            tooltip=['priority', 'count']
        ).properties(
            title='Distribución por Prioridad',
            height=400
        )
        
        st.altair_chart(donut_chart, use_container_width=True)
        
        # Análisis de prioridad por categoría
        if 'category_filled' in df_stats.columns:
            priority_category = df_stats.groupby(['category_filled', 'priority']).size().reset_index(name='count')
            
            stacked_bar = alt.Chart(priority_category).mark_bar().encode(
                x=alt.X('category_filled:N', title='Categoría'),
                y=alt.Y('count:Q', title='Número de Reuniones'),
                color=alt.Color(
                    'priority:N',
                    scale=alt.Scale(
                        domain=[Priority.ALTA.value, Priority.MEDIA.value, Priority.BAJA.value],
                        range=['#ff4444', '#ffaa00', '#4444ff']
                    )
                ),
                tooltip=['category_filled', 'priority', 'count']
            ).properties(
                title='Prioridades por Categoría',
                height=400
            )
            
            st.altair_chart(stacked_bar, use_container_width=True)
    
    with tab4:
        # Análisis de participación
        all_attendees = []
        for attendees_str in df_stats['attendees'].dropna():
            attendees_list = [a.strip() for a in attendees_str.split('\n') if a.strip()]
            all_attendees.extend(attendees_list)
        
        if all_attendees:
            attendee_counts = pd.Series(all_attendees).value_counts().head(10)
            
            attendee_df = pd.DataFrame({
                'attendee': attendee_counts.index,
                'meetings': attendee_counts.values
            })
            
            attendee_chart = alt.Chart(attendee_df).mark_bar(color='teal').encode(
                x=alt.X('meetings:Q', title='Número de Reuniones'),
                y=alt.Y('attendee:N', title='Asistente', sort='-x'),
                tooltip=['attendee', 'meetings']
            ).properties(
                title='Top 10 Asistentes más Frecuentes',
                height=400
            )
            
            st.altair_chart(attendee_chart, use_container_width=True)
            
            # Métricas de participación
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("Total Asistentes Únicos", len(set(all_attendees)))
            
            with col2:
                avg_attendees = len(all_attendees) / len(df_stats)
                st.metric("Promedio Asistentes/Reunión", f"{avg_attendees:.1f}")
            
            with col3:
                meetings_with_attendees = df_stats['attendees'].notna().sum()
                pct_with_attendees = (meetings_with_attendees / len(df_stats)) * 100
                st.metric("% Reuniones con Asistentes", f"{pct_with_attendees:.0f}%")
        else:
            st.info("No hay datos de asistentes para analizar")

# --- Inicialización de Session State ---
if 'editing_meeting_id' not in st.session_state:
    st.session_state.editing_meeting_id = None
if 'meeting_to_edit' not in st.session_state:
    st.session_state.meeting_to_edit = None
if 'action_items_before_edit' not in st.session_state:
    st.session_state.action_items_before_edit = pd.DataFrame()
if 'last_filter_key' not in st.session_state:
    st.session_state.last_filter_key = None
if 'timezone' not in st.session_state:
    st.session_state.timezone = 'UTC'

# --- Header Principal ---
col_logo, col_title = st.columns([1, 11])
with col_logo:
    st.image("https://cdn-icons-png.flaticon.com/512/10828/10828783.png", width=60)
with col_title:
    st.title("📋 Gestor de Notas y Reuniones")
    st.caption("Sistema integral para gestión de reuniones y seguimiento de tareas")

# --- Tabs Principales ---
tab_add_edit, tab_view, tab_actions, tab_stats, tab_settings = st.tabs([
    "➕ Añadir/Editar",
    "🗓️ Ver Registros",
    "🎯 Tracker de Acciones",
    "📊 Estadísticas",
    "⚙️ Configuración"
])

# --- Tab: Añadir/Editar ---
with tab_add_edit:
    # Cargar datos si estamos editando
    if st.session_state.editing_meeting_id and not st.session_state.meeting_to_edit:
        try:
            df_edit = conn.query(
                "SELECT * FROM meetings WHERE meeting_id = :id",
                params={"id": st.session_state.editing_meeting_id},
                ttl=0
            )
            if not df_edit.empty:
                st.session_state.meeting_to_edit = df_edit.iloc[0].to_dict()
            else:
                st.warning("No se encontró la reunión para editar")
                st.session_state.editing_meeting_id = None
                st.rerun()
        except Exception as e:
            st.error(f"Error al cargar datos: {str(e)}")
            st.session_state.editing_meeting_id = None
            st.rerun()
    
    render_meeting_form(st.session_state.meeting_to_edit)

# --- Tab: Ver Registros ---
with tab_view:
    st.header("🔎 Explorador de Reuniones")
    
    # Filtros
    with st.container(border=True):
        st.subheader("🔍 Filtros")
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            search_query = st.text_input(
                "Buscar",
                placeholder="Título, asistentes, contenido...",
                help="Busca en todos los campos de texto"
            )
            
            categories = get_distinct_categories()
            filter_category = st.selectbox(
                "Categoría",
                options=categories,
                help="Filtra por categoría específica"
            )
        
        with col2:
            date_from = st.date_input(
                "Desde",
                value=None,
                format="YYYY-MM-DD",
                help="Fecha inicial del rango"
            )
            
            date_to = st.date_input(
                "Hasta",
                value=None,
                format="YYYY-MM-DD",
                help="Fecha final del rango"
            )
        
        with col3:
            priority_options = ["Todas"] + [p.value for p in Priority]
            filter_priority = st.selectbox(
                "Prioridad",
                options=priority_options,
                help="Filtra por nivel de prioridad"
            )
            
            sort_options = {
                "Fecha de reunión": "meeting_date",
                "Título": "title",
                "Categoría": "category",
                "Prioridad": "priority",
                "Fecha de creación": "created_at"
            }
            
            sort_by = st.selectbox(
                "Ordenar por",
                options=list(sort_options.keys()),
                help="Campo para ordenar resultados"
            )
            
            sort_ascending = st.checkbox(
                "Orden ascendente",
                value=False,
                help="Marca para orden ascendente, desmarca para descendente"
            )
        
        col_refresh, col_export = st.columns([1, 1])
        
        with col_refresh:
            if st.button("🔄 Aplicar Filtros", use_container_width=True, type="primary"):
                st.rerun()
    
    # Obtener datos
    filters = {
        "search_term": search_query,
        "date_from": date_from,
        "date_to": date_to,
        "category": filter_category,
        "priority": filter_priority,
        "sort_by": sort_options[sort_by],
        "ascending": sort_ascending
    }
    
    df_meetings = MeetingRepository.read(filters)
    
    # Mostrar resultados
    if df_meetings.empty:
        st.info("📭 No se encontraron reuniones con los filtros aplicados")
    else:
        # Contador y exportación masiva
        col_count, col_export_all = st.columns([3, 1])
        
        with col_count:
            st.metric("Reuniones encontradas", len(df_meetings))
        
        with col_export_all:
            if st.button("📥 Exportar Todo", use_container_width=True):
                # Crear ZIP con todas las reuniones
                import zipfile
                import io
                
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    for _, meeting in df_meetings.iterrows():
                        # Añadir Markdown
                        md_content = ExportService.to_markdown(meeting)
                        zip_file.writestr(
                            f"markdown/reunion_{meeting['meeting_id']}.md",
                            md_content
                        )
                        
                        # Añadir ICS
                        ics_content = ExportService.to_ics(meeting)
                        zip_file.writestr(
                            f"calendar/reunion_{meeting['meeting_id']}.ics",
                            ics_content
                        )
                
                st.download_button(
                    "💾 Descargar ZIP",
                    zip_buffer.getvalue(),
                    f"reuniones_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                    "application/zip",
                    use_container_width=True
                )
        
        st.markdown("---")
        
        # Mostrar reuniones
        for _, meeting in df_meetings.iterrows():
            render_meeting_card(meeting)

# --- Tab: Tracker de Acciones ---
with tab_actions:
    st.header("🎯 Gestión Global de Tareas")
    
    df_all_actions = get_all_action_items()
    
    if df_all_actions.empty:
        st.info("📭 No hay puntos de acción registrados en ninguna reunión")
    else:
        # Filtros de acciones
        with st.container(border=True):
            st.subheader("🔍 Filtros de Tareas")
            
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                unique_assignees = ["Todos"] + sorted(
                    list(df_all_actions['assignee'].dropna().unique())
                )
                filter_assignee = st.selectbox(
                    "Responsable",
                    unique_assignees,
                    help="Filtra por persona asignada"
                )
            
            with col2:
                status_options = ["Todos"] + [s.value for s in TaskStatus]
                filter_status = st.selectbox(
                    "Estado",
                    status_options,
                    help="Filtra por estado de la tarea"
                )
            
            with col3:
                search_task = st.text_input(
                    "Buscar en tareas",
                    placeholder="Palabra clave...",
                    help="Busca en el texto de las tareas"
                )
            
            with col4:
                # Filtro por vencimiento
                vencimiento_options = [
                    "Todas",
                    "Vencidas",
                    "Vencen hoy",
                    "Vencen esta semana",
                    "Sin fecha"
                ]
                filter_vencimiento = st.selectbox(
                    "Vencimiento",
                    vencimiento_options,
                    help="Filtra por fecha de vencimiento"
                )
        
        # Aplicar filtros
        filtered_df = df_all_actions.copy()
        
        if filter_assignee != "Todos":
            filtered_df = filtered_df[filtered_df['assignee'] == filter_assignee]
        
        if filter_status != "Todos":
            filtered_df = filtered_df[filtered_df['status'] == filter_status]
        
        if search_task:
            filtered_df = filtered_df[
                filtered_df['task'].str.contains(search_task, case=False, na=False)
            ]
        
        # Filtro por vencimiento
        today = date.today()
        if filter_vencimiento == "Vencidas":
            filtered_df = filtered_df[filtered_df['is_overdue'] == True]
        elif filter_vencimiento == "Vencen hoy":
            filtered_df = filtered_df[
                filtered_df['due_date_str'] == today.strftime('%Y-%m-%d')
            ]
        elif filter_vencimiento == "Vencen esta semana":
            week_end = today + timedelta(days=(6 - today.weekday()))
            filtered_df = filtered_df[
                (filtered_df['due_date_str'] >= today.strftime('%Y-%m-%d')) &
                (filtered_df['due_date_str'] <= week_end.strftime('%Y-%m-%d'))
            ]
        elif filter_vencimiento == "Sin fecha":
            filtered_df = filtered_df[filtered_df['due_date_str'].isna()]
        
        # Métricas
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("Total Tareas", len(filtered_df))
        
        with col2:
            completed = len(filtered_df[filtered_df['status'] == TaskStatus.COMPLETADO.value])
            completion_rate = (completed / len(filtered_df) * 100) if len(filtered_df) > 0 else 0
            st.metric("Completadas", f"{completed} ({completion_rate:.0f}%)")
        
        with col3:
            overdue = len(filtered_df[filtered_df['is_overdue'] == True])
            st.metric("Vencidas", overdue, delta_color="inverse")
        
        with col4:
            with_assignee = len(filtered_df[filtered_df['assignee'].notna()])
            st.metric("Con Responsable", with_assignee)
        
        st.markdown("---")
        
        # Editor de tareas
        if not filtered_df.empty:
            st.subheader("📝 Editor de Estados")
            st.caption("Modifica el estado de las tareas directamente en la tabla")
            
            # Preparar DataFrame para edición
            df_for_editor = filtered_df.copy()
            df_for_editor['editor_id'] = df_for_editor.apply(
                lambda r: f"{r['meeting_id']}_{hash(r['original_full_text'])}",
                axis=1
            )
            
            # Guardar estado antes de edición
            current_filter_key = f"{filter_assignee}_{filter_status}_{search_task}_{filter_vencimiento}"
            if st.session_state.last_filter_key != current_filter_key:
                st.session_state.action_items_before_edit = df_for_editor.set_index('editor_id').copy()
                st.session_state.last_filter_key = current_filter_key
            
            # Configuración de columnas
            column_config = {
                "task": st.column_config.TextColumn(
                    "Tarea",
                    width="large",
                    disabled=True,
                    help="Descripción de la tarea"
                ),
                "assignee": st.column_config.TextColumn(
                    "Responsable",
                    disabled=True,
                    help="Persona asignada"
                ),
                "due_date_str": st.column_config.TextColumn(
                    "Fecha Límite",
                    disabled=True,
                    help="Fecha de vencimiento"
                ),
                "status": st.column_config.SelectboxColumn(
                    "Estado",
                    options=[s.value for s in TaskStatus] + ["Quitar Estado"],
                    required=False,
                    help="Estado actual de la tarea"
                ),
                "meeting_title": st.column_config.TextColumn(
                    "Reunión",
                    disabled=True,
                    help="Reunión de origen"
                ),
                "meeting_id": None,
                "original_full_text": None,
                "text_for_reserialization": None,
                "is_overdue": None,
                "editor_id": None
            }
            
            # Editor de datos
            edited_df = st.data_editor(
                df_for_editor,
                column_config=column_config,
                use_container_width=True,
                hide_index=True,
                key="action_items_editor",
                num_rows="fixed"
            )
            
            # Detectar y aplicar cambios
            if not edited_df.empty and 'action_items_before_edit' in st.session_state:
                edited_df_indexed = edited_df.set_index('editor_id')
                
                for editor_id, edited_row in edited_df_indexed.iterrows():
                    if editor_id in st.session_state.action_items_before_edit.index:
                        original_row = st.session_state.action_items_before_edit.loc[editor_id]
                        
                        if edited_row['status'] != original_row['status']:
                            success = ActionItemService.update_status(
                                original_row['meeting_id'],
                                original_row['original_full_text'],
                                edited_row['status']
                            )
                            
                            if success:
                                st.session_state.action_items_before_edit.loc[editor_id, 'status'] = edited_row['status']
                                st.rerun()
                            else:
                                st.rerun()
                                break
            
            # Vista Kanban alternativa
            with st.expander("🎯 Vista Kanban", expanded=False):
                kanban_cols = st.columns(len(TaskStatus))
                
                for idx, status in enumerate(TaskStatus):
                    with kanban_cols[idx]:
                        st.markdown(f"#### {status.value}")
                        
                        status_tasks = filtered_df[filtered_df['status'] == status.value]
                        
                        if not status_tasks.empty:
                            for _, task in status_tasks.iterrows():
                                with st.container(border=True):
                                    st.markdown(f"**{task['task'][:50]}...**" if len(task['task']) > 50 else f"**{task['task']}**")
                                    
                                    if task['assignee']:
                                        st.caption(f"👤 {task['assignee']}")
                                    
                                    if task['due_date_str']:
                                        if task['is_overdue']:
                                            st.caption(f"📅 ~~{task['due_date_str']}~~ 🔴")
                                        else:
                                            st.caption(f"📅 {task['due_date_str']}")
                                    
                                    st.caption(f"📋 {task['meeting_title'][:30]}...")
                        else:
                            st.caption("_Sin tareas_")
        else:
            st.info("No hay tareas que coincidan con los filtros aplicados")

# --- Tab: Estadísticas ---
with tab_stats:
    st.header("📊 Dashboard de Análisis")
    
    # Obtener todos los datos para estadísticas
    df_all = MeetingRepository.read({"sort_by": "meeting_date", "ascending": True})
    
    if df_all.empty:
        st.info("📊 No hay datos suficientes para generar estadísticas")
    else:
        # Selector de rango de fechas para análisis
        col1, col2 = st.columns(2)
        with col1:
            analysis_start = st.date_input(
                "Analizar desde",
                value=df_all['meeting_date'].min(),
                help="Fecha inicial para el análisis"
            )
        with col2:
            analysis_end = st.date_input(
                "Analizar hasta",
                value=df_all['meeting_date'].max(),
                help="Fecha final para el análisis"
            )
        
        # Filtrar datos por rango
        df_analysis = df_all[
            (pd.to_datetime(df_all['meeting_date']).dt.date >= analysis_start) &
            (pd.to_datetime(df_all['meeting_date']).dt.date <= analysis_end)
        ].copy()
        
        if df_analysis.empty:
            st.warning("No hay datos en el rango seleccionado")
        else:
            render_statistics(df_analysis)

# --- Tab: Configuración ---
with tab_settings:
    st.header("⚙️ Configuración del Sistema")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("🌍 Preferencias Regionales")
        
        # Zona horaria
        import pytz
        timezones = pytz.all_timezones
        current_tz = st.session_state.get('timezone', 'UTC')
        
        selected_tz = st.selectbox(
            "Zona Horaria",
            options=timezones,
            index=timezones.index(current_tz),
            help="Zona horaria para mostrar fechas y horas"
        )
        
        if selected_tz != current_tz:
            st.session_state.timezone = selected_tz
            st.success(f"Zona horaria actualizada a {selected_tz}")
        
        st.subheader("🔔 Notificaciones")
        
        # Configuración de recordatorios
        reminder_settings = {
            "remind_overdue": st.checkbox(
                "Recordar tareas vencidas",
                value=True,
                help="Enviar recordatorios para tareas pasadas de fecha"
            ),
            "remind_today": st.checkbox(
                "Recordar tareas de hoy",
                value=True,
                help="Enviar recordatorios para tareas que vencen hoy"
            ),
            "remind_tomorrow": st.checkbox(
                "Recordar tareas de mañana",
                value=True,
                help="Enviar recordatorios para tareas que vencen mañana"
            ),
            "remind_week": st.checkbox(
                "Recordar tareas de la semana",
                value=False,
                help="Enviar recordatorios para tareas que vencen esta semana"
            )
        }
        
        # Horario de recordatorios
        reminder_time = st.time_input(
            "Hora de envío de recordatorios",
            value=datetime.strptime("09:00", "%H:%M").time(),
            help="Hora del día para enviar recordatorios automáticos"
        )
    
    with col2:
        st.subheader("🗄️ Base de Datos")
        
        # Información de la base de datos
        try:
            db_info = conn.query("SELECT COUNT(*) as total FROM meetings").iloc[0]['total']
            st.metric("Total de Reuniones", db_info)
            
            # Tamaño de la base de datos
            db_size_query = """
                SELECT 
                    table_schema AS 'Database',
                    SUM(data_length + index_length) / 1024 / 1024 AS 'Size (MB)'
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                GROUP BY table_schema
            """
            db_size = conn.query(db_size_query)
            if not db_size.empty:
                st.metric("Tamaño de BD", f"{db_size.iloc[0]['Size (MB)']:.2f} MB")
        except Exception as e:
            st.error(f"Error al obtener información de BD: {str(e)}")
        
        st.subheader("🧹 Mantenimiento")
        
        # Limpieza de datos antiguos
        with st.expander("Limpieza de Datos Antiguos"):
            st.warning("⚠️ Esta acción es irreversible")
            
            months_to_keep = st.slider(
                "Mantener reuniones de los últimos (meses)",
                min_value=1,
                max_value=24,
                value=12,
                help="Las reuniones más antiguas serán eliminadas"
            )
            
            cutoff_date = datetime.now() - timedelta(days=months_to_keep * 30)
            
            # Preview de lo que se eliminará
            old_meetings = conn.query(
                "SELECT COUNT(*) as count FROM meetings WHERE meeting_date < :cutoff",
                params={"cutoff": cutoff_date}
            )
            
            if old_meetings.iloc[0]['count'] > 0:
                st.info(f"Se eliminarán {old_meetings.iloc[0]['count']} reuniones anteriores a {cutoff_date.strftime('%Y-%m-%d')}")
                
                if st.button("🗑️ Ejecutar Limpieza", type="secondary"):
                    try:
                        with conn.session as session:
                            result = session.execute(
                                text("DELETE FROM meetings WHERE meeting_date < :cutoff"),
                                {"cutoff": cutoff_date}
                            )
                            session.commit()
                        
                        st.success(f"✅ Se eliminaron {result.rowcount} reuniones antiguas")
                        MeetingRepository.clear_cache()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error al limpiar datos: {str(e)}")
            else:
                st.info("No hay reuniones para limpiar con los criterios actuales")
        
        # Exportación completa
        st.subheader("💾 Respaldo de Datos")
        
        if st.button("📥 Generar Respaldo Completo", use_container_width=True):
            try:
                # Obtener todos los datos
                all_data = conn.query("SELECT * FROM meetings ORDER BY meeting_date")
                
                # Crear archivo JSON
                backup_data = {
                    "export_date": datetime.now().isoformat(),
                    "version": "2.0",
                    "meetings": all_data.to_dict('records')
                }
                
                # Convertir a JSON con formato legible
                json_str = json.dumps(backup_data, indent=2, default=str)
                
                st.download_button(
                    "💾 Descargar Respaldo JSON",
                    json_str,
                    f"backup_meetings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                    "application/json",
                    use_container_width=True
                )
                
                st.success("✅ Respaldo generado exitosamente")
            except Exception as e:
                st.error(f"Error al generar respaldo: {str(e)}")

# --- Sidebar ---
with st.sidebar:
    st.header("🚀 Acciones Rápidas")
    
    # Botón de refresco
    if st.button("🔄 Refrescar Todo", use_container_width=True, type="primary"):
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()
    
    st.markdown("---")
    
    # Recordatorios
    st.subheader("📢 Sistema de Recordatorios")
    
    webhook_url = st.secrets.get("google_chat_webhook_url", "")
    
    if not webhook_url or webhook_url == "TU_URL_DE_WEBHOOK_AQUÍ":
        st.warning("⚠️ Webhook no configurado")
        st.caption("Configura `google_chat_webhook_url` en `.streamlit/secrets.toml`")
    else:
        st.success("✅ Webhook configurado")
        
        if st.button("📤 Enviar Recordatorios Ahora", use_container_width=True):
            with st.spinner("Procesando recordatorios..."):
                notification_service = NotificationService(webhook_url)
                notification_service.check_and_send_reminders()
    
    st.markdown("---")
    
    # Estadísticas rápidas
    st.subheader("📊 Resumen Rápido")
    
    try:
        # Reuniones de hoy
        today_meetings = conn.query(
            "SELECT COUNT(*) as count FROM meetings WHERE DATE(meeting_date) = CURDATE()"
        ).iloc[0]['count']
        
        # Tareas pendientes
        all_actions = get_all_action_items()
        pending_tasks = len(all_actions[
            all_actions['status'].isin([TaskStatus.PENDIENTE.value, TaskStatus.EN_PROGRESO.value])
        ]) if not all_actions.empty else 0
        
        # Tareas vencidas
        overdue_tasks = len(all_actions[all_actions['is_overdue'] == True]) if not all_actions.empty else 0
        
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Reuniones Hoy", today_meetings)
            st.metric("Tareas Pendientes", pending_tasks)
        with col2:
            st.metric("Próxima Reunión", "Ver calendario")
            st.metric("Tareas Vencidas", overdue_tasks, delta_color="inverse")
    except Exception as e:
        st.error(f"Error al cargar resumen: {str(e)}")
    
    st.markdown("---")
    
    # Información del sistema
    st.subheader("ℹ️ Información")
    
    st.caption("**Versión:** 2.0.0")
    st.caption("**Última actualización:** " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    
    with st.expander("📚 Dependencias"):
        dependencies = [
            "streamlit",
            "pandas",
            "sqlalchemy",
            "streamlit-quill",
            "ics",
            "altair",
            "requests",
            "pytz"
        ]
        for dep in dependencies:
            st.caption(f"• {dep}")
    
    with st.expander("🔗 Enlaces Útiles"):
        st.markdown("""
        - [📖 Documentación](https://github.com/tu-usuario/gestor-notas/wiki)
        - [🐛 Reportar Bug](https://github.com/tu-usuario/gestor-notas/issues)
        - [💡 Sugerir Mejora](https://github.com/tu-usuario/gestor-notas/discussions)
        - [📧 Contacto](mailto:soporte@tudominio.com)
        """)

# --- Footer ---
st.markdown("---")
st.caption(
    "Gestor de Notas v2.0 | "
    f"© {datetime.now().year} Tu Empresa | "
    "Desarrollado con ❤️ usando Streamlit"
)
