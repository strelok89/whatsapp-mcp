import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import audio

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
WHATSAPP_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'whatsapp.db')
WHATSAPP_API_BASE_URL = "http://localhost:8080/api"

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None
    unread_count: int = 0

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]


def _open_whatsapp_db() -> Optional[sqlite3.Connection]:
    """Open whatsmeow's whatsapp.db in read-only mode if it exists."""
    if not os.path.exists(WHATSAPP_DB_PATH):
        return None
    try:
        return sqlite3.connect(f"file:{WHATSAPP_DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _chats_has_unread_column(conn: sqlite3.Connection) -> bool:
    """Check whether chats.unread_count exists (safe before bridge migration)."""
    rows = conn.execute("PRAGMA table_info(chats)").fetchall()
    return any(row[1] == "unread_count" for row in rows)


def _lookup_lid_pn(wa_conn: Optional[sqlite3.Connection], user: str, domain: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (pn, lid) for a user ID using whatsmeow_lid_map."""
    if not wa_conn or not user:
        return (user if domain == "s.whatsapp.net" else None, user if domain == "lid" else None)

    pn: Optional[str] = user if domain == "s.whatsapp.net" else None
    lid: Optional[str] = user if domain == "lid" else None

    if domain in ("lid", None):
        row = wa_conn.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ? LIMIT 1", (user,)).fetchone()
        if row and row[0]:
            pn = row[0]
            lid = user
    if not pn and domain is None:
        pn = user
    if pn and not lid:
        row = wa_conn.execute("SELECT lid FROM whatsmeow_lid_map WHERE pn = ? LIMIT 1", (pn,)).fetchone()
        if row and row[0]:
            lid = row[0]
    return pn, lid


def _resolve_identity(
    identifier: str,
    chats_name: Optional[str] = None,
    wa_conn: Optional[sqlite3.Connection] = None,
    msg_conn: Optional[sqlite3.Connection] = None,
) -> Tuple[str, str, str]:
    """Resolve a JID or bare ID to (display_name, phone_number, canonical_jid).

    Priority for display_name:
      saved contact name -> push_name -> non-numeric chats.name -> phone_number -> lid/user
    """
    if not identifier:
        return ("", "", "")

    if identifier.endswith("@g.us") or identifier == "status@broadcast":
        user = identifier.split("@")[0]
        return (chats_name or identifier, user, identifier)

    user = identifier.split("@")[0] if "@" in identifier else identifier
    domain = identifier.split("@")[1] if "@" in identifier else None

    own_wa = False
    if wa_conn is None:
        wa_conn = _open_whatsapp_db()
        own_wa = wa_conn is not None

    try:
        pn, lid = _lookup_lid_pn(wa_conn, user, domain)
        phone_number = pn or user
        canonical_jid = f"{pn}@s.whatsapp.net" if pn else (identifier if "@" in identifier else f"{user}@s.whatsapp.net")

        saved_name: Optional[str] = None
        push_name: Optional[str] = None

        if wa_conn is not None:
            candidates: List[str] = []
            if pn:
                candidates.append(f"{pn}@s.whatsapp.net")
            if "@" in identifier and identifier not in candidates:
                candidates.append(identifier)
            if lid:
                lid_jid = f"{lid}@lid"
                if lid_jid not in candidates:
                    candidates.append(lid_jid)

            if candidates:
                placeholders = ",".join("?" for _ in candidates)
                rows = wa_conn.execute(
                    f"""
                    SELECT their_jid, full_name, first_name, business_name, push_name
                    FROM whatsmeow_contacts
                    WHERE their_jid IN ({placeholders})
                    """,
                    tuple(candidates),
                ).fetchall()
                by_jid = {r[0]: r for r in rows}
                for cand in candidates:
                    row = by_jid.get(cand)
                    if not row:
                        continue
                    _, full_name, first_name, business_name, p_name = row
                    if not saved_name:
                        saved_name = full_name or first_name or business_name or None
                    if not push_name and p_name:
                        push_name = p_name

        if saved_name:
            return (saved_name, phone_number, canonical_jid)
        if push_name:
            return (push_name, phone_number, canonical_jid)

        if chats_name is None and msg_conn is not None:
            lookup_jids = [identifier] if "@" in identifier else [f"{user}@s.whatsapp.net", f"{user}@lid"]
            if pn:
                lookup_jids.append(f"{pn}@s.whatsapp.net")
            if lid:
                lookup_jids.append(f"{lid}@lid")
            placeholders = ",".join("?" for _ in lookup_jids)
            row = msg_conn.execute(
                f"SELECT name FROM chats WHERE jid IN ({placeholders}) AND name IS NOT NULL AND name != '' LIMIT 1",
                tuple(lookup_jids),
            ).fetchone()
            if row:
                chats_name = row[0]

        if chats_name and chats_name != user and chats_name != lid:
            return (chats_name, phone_number, canonical_jid)

        if pn:
            return (pn, phone_number, canonical_jid)

        return (user, phone_number, canonical_jid)
    finally:
        if own_wa and wa_conn is not None:
            wa_conn.close()


def _expand_sender_variants(identifier: str, wa_conn: Optional[sqlite3.Connection]) -> List[str]:
    """Return all equivalent JID/user representations (PN and LID) for querying messages/chats."""
    if not identifier:
        return []
    user = identifier.split("@")[0] if "@" in identifier else identifier
    domain = identifier.split("@")[1] if "@" in identifier else None
    pn, lid = _lookup_lid_pn(wa_conn, user, domain)
    variants: List[str] = []
    for val in (
        identifier,
        user,
        pn,
        f"{pn}@s.whatsapp.net" if pn else None,
        lid,
        f"{lid}@lid" if lid else None,
    ):
        if val and val not in variants:
            variants.append(val)
    return variants


def get_sender_name(sender_jid: str) -> str:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        wa_conn = _open_whatsapp_db()
        try:
            name, _, _ = _resolve_identity(sender_jid, wa_conn=wa_conn, msg_conn=conn)
            return name or sender_jid
        finally:
            if wa_conn is not None:
                wa_conn.close()
            conn.close()
    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid

def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    
    if show_chat_info and (message.chat_name or message.chat_jid):
        chat_display = message.chat_name
        if message.chat_jid and not message.chat_jid.endswith("@g.us"):
            resolved_chat, _, _ = _resolve_identity(message.chat_jid, chats_name=message.chat_name)
            if resolved_chat:
                chat_display = resolved_chat
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {chat_display} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    
    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output
    
    for message in messages:
        output += format_message(message, show_chat_info)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        
        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            wa_conn = _open_whatsapp_db()
            try:
                variants = _expand_sender_variants(sender_phone_number, wa_conn)
            finally:
                if wa_conn is not None:
                    wa_conn.close()
            placeholders = ",".join("?" for _ in variants)
            where_clauses.append(f"messages.sender IN ({placeholders})")
            params.extend(variants)
            
        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            )
            result.append(message)
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
            
        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8]
        )
        
        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))
        
        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))
        
        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def _build_chat(chat_data: tuple, wa_conn: Optional[sqlite3.Connection], msg_conn: sqlite3.Connection) -> Chat:
    jid, raw_name, last_time, last_msg, last_sender, last_is_from_me, unread_count = chat_data
    display_name, _, _ = _resolve_identity(jid, chats_name=raw_name, wa_conn=wa_conn, msg_conn=msg_conn)
    resolved_last_sender = last_sender
    if last_sender:
        resolved_last_sender, _, _ = _resolve_identity(last_sender, wa_conn=wa_conn, msg_conn=msg_conn)
    return Chat(
        jid=jid,
        name=display_name,
        last_message_time=datetime.fromisoformat(last_time) if last_time else None,
        last_message=last_msg,
        last_sender=resolved_last_sender,
        last_is_from_me=last_is_from_me,
        unread_count=int(unread_count or 0),
    )


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
    unread_only: bool = False,
    min_unread: Optional[int] = None,
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        has_unread = _chats_has_unread_column(conn)
        unread_expr = "COALESCE(chats.unread_count, 0)" if has_unread else "0"

        if include_last_message:
            query_parts = [f"""
                SELECT 
                    chats.jid,
                    chats.name,
                    chats.last_message_time,
                    messages.content as last_message,
                    messages.sender as last_sender,
                    messages.is_from_me as last_is_from_me,
                    {unread_expr} as unread_count
                FROM chats
                LEFT JOIN messages ON chats.jid = messages.chat_jid 
                    AND chats.last_message_time = messages.timestamp
            """]
        else:
            query_parts = [f"""
                SELECT 
                    chats.jid,
                    chats.name,
                    chats.last_message_time,
                    NULL as last_message,
                    NULL as last_sender,
                    NULL as last_is_from_me,
                    {unread_expr} as unread_count
                FROM chats
            """]
            
        where_clauses = []
        params = []
        
        if query:
            where_clauses.append("(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])

        if unread_only:
            where_clauses.append(f"{unread_expr} > 0")
        if min_unread is not None:
            where_clauses.append(f"{unread_expr} >= ?")
            params.append(min_unread)
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = page * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        
        wa_conn = _open_whatsapp_db()
        try:
            return [_build_chat(chat_data, wa_conn, conn) for chat_data in chats]
        finally:
            if wa_conn is not None:
                wa_conn.close()
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name, push name, phone number, or LID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        wa_conn = _open_whatsapp_db()
        try:
            search_pattern = '%' + query + '%'
            seen: dict[str, Contact] = {}

            if wa_conn is not None:
                wa_rows = wa_conn.execute(
                    """
                    SELECT DISTINCT
                        c.their_jid,
                        COALESCE(NULLIF(c.full_name, ''), NULLIF(c.first_name, ''), NULLIF(c.business_name, ''), NULLIF(c.push_name, '')) as contact_name
                    FROM whatsmeow_contacts c
                    LEFT JOIN whatsmeow_lid_map lm_pn ON c.their_jid = lm_pn.pn || '@s.whatsapp.net'
                    LEFT JOIN whatsmeow_lid_map lm_lid ON c.their_jid = lm_lid.lid || '@lid'
                    WHERE
                        LOWER(COALESCE(c.full_name, '')) LIKE LOWER(?)
                        OR LOWER(COALESCE(c.first_name, '')) LIKE LOWER(?)
                        OR LOWER(COALESCE(c.push_name, '')) LIKE LOWER(?)
                        OR LOWER(COALESCE(c.business_name, '')) LIKE LOWER(?)
                        OR LOWER(c.their_jid) LIKE LOWER(?)
                        OR LOWER(COALESCE(lm_pn.lid, '')) LIKE LOWER(?)
                        OR LOWER(COALESCE(lm_lid.pn, '')) LIKE LOWER(?)
                    LIMIT 100
                    """,
                    (search_pattern,) * 7,
                ).fetchall()
                for their_jid, _ in wa_rows:
                    name, phone, canonical_jid = _resolve_identity(their_jid, wa_conn=wa_conn, msg_conn=conn)
                    seen.setdefault(canonical_jid, Contact(phone_number=phone, name=name, jid=canonical_jid))

                lid_rows = wa_conn.execute(
                    """
                    SELECT lid, pn FROM whatsmeow_lid_map
                    WHERE LOWER(lid) LIKE LOWER(?) OR LOWER(pn) LIKE LOWER(?)
                    LIMIT 100
                    """,
                    (search_pattern, search_pattern),
                ).fetchall()
                for lid, pn in lid_rows:
                    name, phone, canonical_jid = _resolve_identity(f"{pn}@s.whatsapp.net", wa_conn=wa_conn, msg_conn=conn)
                    seen.setdefault(canonical_jid, Contact(phone_number=phone, name=name, jid=canonical_jid))

            chat_rows = conn.execute(
                """
                SELECT DISTINCT jid, name
                FROM chats
                WHERE
                    (LOWER(COALESCE(name, '')) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
                    AND jid NOT LIKE '%@g.us'
                    AND jid != 'status@broadcast'
                ORDER BY name, jid
                LIMIT 50
                """,
                (search_pattern, search_pattern),
            ).fetchall()
            for jid, raw_name in chat_rows:
                name, phone, canonical_jid = _resolve_identity(jid, chats_name=raw_name, wa_conn=wa_conn, msg_conn=conn)
                if canonical_jid not in seen:
                    seen[canonical_jid] = Contact(phone_number=phone, name=name, jid=canonical_jid)

            result = sorted(seen.values(), key=lambda c: ((c.name or "").lower(), c.jid))
            return result[:50]
        finally:
            if wa_conn is not None:
                wa_conn.close()
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        wa_conn = _open_whatsapp_db()
        try:
            has_unread = _chats_has_unread_column(conn)
            unread_expr = "COALESCE(c.unread_count, 0)" if has_unread else "0"
            variants = _expand_sender_variants(jid, wa_conn)
            placeholders = ",".join("?" for _ in variants)
            
            cursor.execute(f"""
                SELECT DISTINCT
                    c.jid,
                    c.name,
                    c.last_message_time,
                    m.content as last_message,
                    m.sender as last_sender,
                    m.is_from_me as last_is_from_me,
                    {unread_expr} as unread_count
                FROM chats c
                JOIN messages m ON c.jid = m.chat_jid
                WHERE m.sender IN ({placeholders}) OR c.jid IN ({placeholders})
                ORDER BY c.last_message_time DESC
                LIMIT ? OFFSET ?
            """, tuple(variants) + tuple(variants) + (limit, page * limit))
            
            chats = cursor.fetchall()
            return [_build_chat(chat_data, wa_conn, conn) for chat_data in chats]
        finally:
            if wa_conn is not None:
                wa_conn.close()
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        wa_conn = _open_whatsapp_db()
        try:
            variants = _expand_sender_variants(jid, wa_conn)
            placeholders = ",".join("?" for _ in variants)
            cursor.execute(f"""
                SELECT 
                    m.timestamp,
                    m.sender,
                    c.name,
                    m.content,
                    m.is_from_me,
                    c.jid,
                    m.id,
                    m.media_type
                FROM messages m
                JOIN chats c ON m.chat_jid = c.jid
                WHERE m.sender IN ({placeholders}) OR c.jid IN ({placeholders})
                ORDER BY m.timestamp DESC
                LIMIT 1
            """, tuple(variants) + tuple(variants))
        finally:
            if wa_conn is not None:
                wa_conn.close()
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        
        return format_message(message)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        has_unread = _chats_has_unread_column(conn)
        unread_expr = "COALESCE(c.unread_count, 0)" if has_unread else "0"
        
        if include_last_message:
            query = f"""
                SELECT 
                    c.jid,
                    c.name,
                    c.last_message_time,
                    m.content as last_message,
                    m.sender as last_sender,
                    m.is_from_me as last_is_from_me,
                    {unread_expr} as unread_count
                FROM chats c
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                    AND c.last_message_time = m.timestamp
                WHERE c.jid = ?
            """
        else:
            query = f"""
                SELECT 
                    c.jid,
                    c.name,
                    c.last_message_time,
                    NULL as last_message,
                    NULL as last_sender,
                    NULL as last_is_from_me,
                    {unread_expr} as unread_count
                FROM chats c
                WHERE c.jid = ?
            """
        
        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        wa_conn = _open_whatsapp_db()
        try:
            return _build_chat(chat_data, wa_conn, conn)
        finally:
            if wa_conn is not None:
                wa_conn.close()
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        wa_conn = _open_whatsapp_db()
        try:
            has_unread = _chats_has_unread_column(conn)
            unread_expr = "COALESCE(c.unread_count, 0)" if has_unread else "0"
            variants = _expand_sender_variants(sender_phone_number, wa_conn)
            like_clauses = " OR ".join("c.jid LIKE ?" for _ in variants)
            like_params = [f"%{v}%" for v in variants]
            
            cursor.execute(f"""
                SELECT 
                    c.jid,
                    c.name,
                    c.last_message_time,
                    m.content as last_message,
                    m.sender as last_sender,
                    m.is_from_me as last_is_from_me,
                    {unread_expr} as unread_count
                FROM chats c
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                    AND c.last_message_time = m.timestamp
                WHERE ({like_clauses}) AND c.jid NOT LIKE '%@g.us'
                LIMIT 1
            """, tuple(like_params))
            
            chat_data = cursor.fetchone()
            if not chat_data:
                return None
            return _build_chat(chat_data, wa_conn, conn)
        finally:
            if wa_conn is not None:
                wa_conn.close()
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None


def get_group_participants(chat_jid: str) -> dict:
    """List participants of a WhatsApp group along with roles and group member tags."""
    if not chat_jid or not chat_jid.endswith("@g.us"):
        return {
            "success": False,
            "message": "chat_jid must be a valid group JID ending with @g.us",
        }

    try:
        response = requests.get(
            f"{WHATSAPP_API_BASE_URL}/group-participants",
            params={"chat_jid": chat_jid},
            timeout=15,
        )
        if response.status_code != 200:
            return {
                "success": False,
                "message": f"Error: HTTP {response.status_code} - {response.text}",
            }

        data = response.json()
        if not data.get("success", False):
            return {
                "success": False,
                "message": data.get("message", "Failed to fetch group participants"),
            }

        participants = data.get("participants") or []
        wa_conn = _open_whatsapp_db()
        msg_conn = sqlite3.connect(MESSAGES_DB_PATH)
        try:
            has_tags_table = bool(
                msg_conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_member_tags'"
                ).fetchone()
            )
            db_tags: dict[str, str] = {}
            if has_tags_table:
                for user_jid, tag in msg_conn.execute(
                    "SELECT user_jid, member_tag FROM group_member_tags WHERE chat_jid = ? AND member_tag != ''",
                    (chat_jid,),
                ).fetchall():
                    db_tags[user_jid] = tag
                    if "@" in user_jid:
                        db_tags[user_jid.split("@")[0]] = tag

            enriched = []
            for p in participants:
                raw_jid = p.get("jid", "")
                lid = p.get("lid", "")
                phone = p.get("phone_number", "")
                name = p.get("name", "")
                role = p.get("role", "member")
                member_tag = p.get("member_tag", "")

                if not name or name == phone or raw_jid.endswith("@lid"):
                    lookup_id = lid or raw_jid or phone
                    res_name, res_phone, res_jid = _resolve_identity(
                        lookup_id, wa_conn=wa_conn, msg_conn=msg_conn
                    )
                    if res_jid and not res_jid.endswith("@lid"):
                        raw_jid = res_jid
                    if res_phone:
                        phone = res_phone
                    if res_name and (not name or name == phone or name == lid.split("@")[0]):
                        name = res_name

                if not member_tag and db_tags:
                    for key in (
                        raw_jid,
                        lid,
                        f"{phone}@s.whatsapp.net" if phone else "",
                        phone,
                        lid.split("@")[0] if "@" in lid else lid,
                    ):
                        if key and key in db_tags:
                            member_tag = db_tags[key]
                            break

                enriched.append({
                    "jid": raw_jid,
                    "phone_number": phone,
                    "lid": lid,
                    "name": name or phone or raw_jid,
                    "role": role,
                    "member_tag": member_tag,
                })

            return {
                "success": True,
                "group_jid": data.get("group_jid", chat_jid),
                "group_name": data.get("group_name", ""),
                "participant_count": len(enriched),
                "participants": enriched,
            }
        finally:
            if wa_conn is not None:
                wa_conn.close()
            msg_conn.close()
    except requests.RequestException as e:
        return {"success": False, "message": f"Request error: {str(e)}"}
    except Exception as e:
        return {"success": False, "message": f"Unexpected error: {str(e)}"}


def edit_message(chat_jid: str, message_id: str, new_message: str) -> Tuple[bool, str]:
    """Edit a previously sent WhatsApp text message."""
    try:
        if not chat_jid:
            return False, "chat_jid must be provided"
        if not message_id:
            return False, "message_id must be provided"
        if not new_message:
            return False, "new_message must be provided"

        url = f"{WHATSAPP_API_BASE_URL}/edit"
        payload = {
            "chat_jid": chat_jid,
            "message_id": message_id,
            "new_message": new_message,
        }
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        return False, f"Error: HTTP {response.status_code} - {response.text}"
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def get_profile_picture(jid: str, preview: bool = False, download: bool = True) -> dict:
    """Fetch (and optionally download) a WhatsApp user's or group's profile picture."""
    if not jid:
        return {"success": False, "message": "jid must be provided"}

    try:
        response = requests.get(
            f"{WHATSAPP_API_BASE_URL}/profile-picture",
            params={
                "jid": jid,
                "preview": "true" if preview else "false",
                "download": "true" if download else "false",
            },
            timeout=15,
        )
        if response.status_code == 200:
            return response.json()
        return {"success": False, "message": f"Error: HTTP {response.status_code} - {response.text}"}
    except requests.RequestException as e:
        return {"success": False, "message": f"Request error: {str(e)}"}
    except json.JSONDecodeError:
        return {"success": False, "message": f"Error parsing response: {response.text}"}
    except Exception as e:
        return {"success": False, "message": f"Unexpected error: {str(e)}"}
