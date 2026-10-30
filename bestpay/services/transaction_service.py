# -*- coding: utf-8 -*-
import logging
from datetime import datetime, time

from odoo import fields

_logger = logging.getLogger(__name__)


class BestPayTransactionQueryService(object):
    """Servicio para consultas de transacciones BestPay vía API.
    
    Centraliza búsqueda, filtrado, paginación y serialización para mantener
    el controlador limpio y permitir extensibilidad por satélites.
    """

    ALLOWED_STATES = ['draft', 'pending', 'done', 'cancel', 'error', 'no_pagado']
    DEFAULT_LIMIT = 25
    MAX_LIMIT = 75
    MAX_DATE_RANGE_DAYS = 31

    def __init__(self, env):
        self.env = env

    def _normalize_datetime(self, value):
        """Normaliza fechas/horas a datetime para búsquedas en Odoo."""
        if not value:
            return False
        if isinstance(value, datetime):
            return value
        if hasattr(value, 'year'):  # date
            dt = datetime.combine(value, time.min)
            return dt
        # Intentar parsear string ISO8601
        if isinstance(value, str):
            value = value.strip()
            # Reemplazar Z por +00:00 si es necesario
            if value.endswith('Z'):
                value = value[:-1] + '+00:00'
            try:
                # Intentar formatos comunes
                for fmt in (
                    '%Y-%m-%dT%H:%M:%S.%f%z',
                    '%Y-%m-%dT%H:%M:%S%z',
                    '%Y-%m-%d %H:%M:%S',
                    '%Y-%m-%dT%H:%M:%S.%f',
                    '%Y-%m-%dT%H:%M:%S',
                    '%Y-%m-%d',
                ):
                    try:
                        dt = datetime.strptime(value.replace('+00:00', ''), fmt.replace('%z', ''))
                        # Si tenía zona horaria simplificada, ignoramos y asumimos local/naive
                        if '%z' in fmt and '+' in value:
                            pass
                        return dt
                    except Exception:
                        continue
                # Fallback
                from dateutil import parser
                try:
                    dt = parser.parse(value)
                    # Convertir a naive si tiene tzinfo
                    if dt.tzinfo:
                        dt = dt.replace(tzinfo=None)
                    return dt
                except Exception:
                    pass
            except Exception:
                pass
        return False

    def _build_domain(self, partner, filters):
        """Construye dominio con scoping obligatorio por cliente."""
        domain = [('bestpay_client_id', '=', partner.id)]
        
        external_reference = filters.get('external_reference')
        if external_reference and isinstance(external_reference, str) and external_reference.strip():
            domain.append(('external_reference', '=', external_reference.strip()))
            return domain  # Si es búsqueda por referencia exacta, no aplicar otros filtros restrictivos
        
        # Filtros opcionales
        state = filters.get('state')
        if state:
            if isinstance(state, str):
                states = [state]
            elif isinstance(state, (list, tuple, set)):
                states = list(state)
            else:
                states = []
            # Validar whitelist
            valid_states = [s for s in states if s in self.ALLOWED_STATES]
            if valid_states:
                if len(valid_states) == 1:
                    domain.append(('state', '=', valid_states[0]))
                else:
                    domain.append(('state', 'in', valid_states))
        
        # Filtros por fecha
        date_from = self._normalize_datetime(filters.get('date_from'))
        date_to = self._normalize_datetime(filters.get('date_to'))
        
        if date_from:
            domain.append(('create_date', '>=', date_from))
        if date_to:
            # Incluir todo el día si es fecha sin hora
            if isinstance(filters.get('date_to'), str) and 'T' not in filters.get('date_to') and ' ' not in filters.get('date_to'):
                # Asumimos fin de día
                date_to = datetime.combine(date_to.date(), time.max)
            domain.append(('create_date', '<=', date_to))
        
        return domain

    def _validate_filters(self, filters):
        """Valida filtros y aplica restricciones de seguridad."""
        filters = filters or {}
        external_reference = filters.get('external_reference')
        has_external_ref = isinstance(external_reference, str) and external_reference.strip()
        date_from = filters.get('date_from')
        date_to = filters.get('date_to')
        has_date_range = bool(date_from or date_to)
        
        # Si NO es búsqueda por external_reference, DEBE tener rango de fechas
        if not has_external_ref and not has_date_range:
            raise ValueError("Debe filtrar por rango de fechas (date_from/date_to) o por external_reference para realizar esta consulta.")
        
        # Validar rango máximo de fechas
        if has_date_range and not has_external_ref:
            df = self._normalize_datetime(date_from)
            dt_norm = self._normalize_datetime(date_to)
            if df and dt_norm:
                delta = dt_norm - df
                if delta.days > self.MAX_DATE_RANGE_DAYS:
                    raise ValueError(f"El rango de fechas no puede exceder {self.MAX_DATE_RANGE_DAYS} días.")
            elif df and not dt_norm:
                # Solo date_from, limitar a 31 días desde ahora
                now = fields.Datetime.now()
                delta = now - df
                if delta.days > self.MAX_DATE_RANGE_DAYS:
                    raise ValueError(f"El rango de fechas no puede exceder {self.MAX_DATE_RANGE_DAYS} días desde date_from.")
            elif dt_norm and not df:
                # Solo date_to, no tiene mucho sentido pero limitar
                pass
        
        # Validar estados
        state = filters.get('state')
        if state:
            if isinstance(state, str):
                states = [state]
            elif isinstance(state, (list, tuple, set)):
                states = list(state)
            else:
                raise ValueError("El filtro state debe ser un string o lista de strings.")
            for s in states:
                if s not in self.ALLOWED_STATES:
                    raise ValueError(f"Estado inválido: {s}. Estados permitidos: {', '.join(self.ALLOWED_STATES)}")
        
        # Validar paginación
        page = filters.get('page', 1)
        limit = filters.get('limit', self.DEFAULT_LIMIT)
        try:
            page = int(page)
            limit = int(limit)
        except Exception:
            raise ValueError("page y limit deben ser números enteros.")
        if page < 1:
            page = 1
        if limit < 1:
            limit = self.DEFAULT_LIMIT
        if limit > self.MAX_LIMIT:
            limit = self.MAX_LIMIT
        
        return {'page': page, 'limit': limit}

    def _serialize_transaction(self, tx):
        """Serializa transacción con proyección mínima (DTO)."""
        payment_link = False
        try:
            # Link público de la transacción. Mercantil no usa 'payment_link':
            # guarda el link de BestPay en 'payment_link_client_mer'. El link del
            # banco ('payment_link_bank_mer') es interno y nunca se expone.
            if getattr(tx, 'payment_link', False):
                payment_link = tx.payment_link
            elif getattr(tx, 'payment_link_client_mer', False):
                payment_link = tx.payment_link_client_mer
        except Exception:
            pass
        
        data = {
            'transaction_id': tx.id,
            'odoo_reference': tx.reference,
            'external_reference': tx.external_reference,
            'uuid_hash': tx.uuid_hash,
            'state': tx.state,
            'transaction_status': tx.state,
            'amount': tx.amount,
            'currency': tx.currency_id.name if tx.currency_id else False,
            'amount_ves': tx.amount_ves,
            'exchange_rate': tx.exchange_rate_bcv,
            'payment_link': payment_link,
            'flow_type': getattr(tx, 'bestpay_flow_type', False),
            'create_date': fields.Datetime.to_string(tx.create_date) if tx.create_date else False,
            'write_date': fields.Datetime.to_string(tx.write_date) if tx.write_date else False,
            # Motivos visibles para el cliente: por qué se venció el link o por qué
            # canceló. Nunca exponen datos internos ni credenciales.
            'unpaid_reason': tx.bestpay_unpaid_reason or False,
            'cancel_reason': tx.bestpay_cancel_reason or False,
        }
        
        # Añadir payment_details normalizados si es necesario (solo campos seguros)
        try:
            if hasattr(tx, 'payment_details') and tx.payment_details:
                # Evitar payloads pesados
                pd = tx.payment_details
                if isinstance(pd, dict):
                    # Filtrar solo campos seguros
                    safe_pd = {}
                    for k in ('payment_link', 'checkout_url', 'redirect_url', 'url', 'message', 'status'):
                        if k in pd:
                            safe_pd[k] = pd[k]
                    if safe_pd:
                        data['payment_details'] = safe_pd
        except Exception:
            pass
        
        if not data.get('payment_link') and data.get('payment_details'):
            data['payment_link'] = data['payment_details'].get('payment_link') or data['payment_details'].get('checkout_url')
        
        return data

    def search_transactions(self, partner, filters=None):
        """Busca transacciones para un cliente con paginación obligatoria."""
        filters = filters or {}
        pagination = self._validate_filters(filters)
        domain = self._build_domain(partner, filters)
        
        Tx = self.env['payment.transaction'].sudo()
        total_count = Tx.search_count(domain)
        
        page = pagination['page']
        limit = pagination['limit']
        offset = (page - 1) * limit
        
        external_reference = filters.get('external_reference')
        has_external_ref = isinstance(external_reference, str) and external_reference.strip()
        
        if has_external_ref:
            # Búsqueda exacta, sin paginación restrictiva
            txs = Tx.search(domain, limit=1, order='create_date desc, id desc')
            transactions = [self._serialize_transaction(tx) for tx in txs]
            return {
                'status': 'success',
                'transactions': transactions,
                'pagination': {
                    'page': 1,
                    'limit': limit,
                    'total_count': total_count,
                    'pages': 1 if total_count > 0 else 0,
                    'has_more': False,
                }
            }
        
        txs = Tx.search(domain, limit=limit, offset=offset, order='create_date desc, id desc')
        transactions = [self._serialize_transaction(tx) for tx in txs]
        
        pages = (total_count + limit - 1) // limit if limit > 0 else 0
        has_more = page < pages
        
        return {
            'status': 'success',
            'transactions': transactions,
            'pagination': {
                'page': page,
                'limit': limit,
                'total_count': total_count,
                'pages': pages,
                'has_more': has_more,
            }
        }

    def get_transaction(self, partner, external_reference):
        """Obtiene una transacción específica por external_reference."""
        if not external_reference or not isinstance(external_reference, str) or not external_reference.strip():
            raise ValueError("external_reference es requerido.")
        
        domain = [
            ('bestpay_client_id', '=', partner.id),
            ('external_reference', '=', external_reference.strip()),
        ]
        Tx = self.env['payment.transaction'].sudo()
        tx = Tx.search(domain, limit=1, order='create_date desc, id desc')
        if not tx:
            raise ValueError("Transacción no encontrada.")
        
        return {
            'status': 'success',
            'transaction': self._serialize_transaction(tx),
        }

    def cancel_transaction(self, partner, external_reference, reason):
        """Cancela una transacción vía API."""
        if not external_reference or not isinstance(external_reference, str) or not external_reference.strip():
            raise ValueError("external_reference es requerido.")
        if not reason or not isinstance(reason, str) or not reason.strip():
            raise ValueError("El motivo de cancelación es requerido.")
        
        domain = [
            ('bestpay_client_id', '=', partner.id),
            ('external_reference', '=', external_reference.strip()),
        ]
        Tx = self.env['payment.transaction'].sudo()
        tx = Tx.search(domain, limit=1, order='create_date desc, id desc')
        if not tx:
            raise ValueError("Transacción no encontrada.")
        
        # Si ya está cancelada, idempotente
        if tx.state == 'cancel':
            return {
                'status': 'success',
                'message': 'La transacción ya se encuentra cancelada.',
                'transaction_status': 'cancel',
                'transaction': self._serialize_transaction(tx),
            }
        
        # No permitir cancelar transacciones pagadas
        if tx.state == 'done':
            raise ValueError("No se puede cancelar una transacción que ya fue pagada.")
        
        # Solo permitir cancelar draft o pending
        if tx.state not in ['draft', 'pending']:
            raise ValueError(f"No se puede cancelar una transacción en estado '{tx.state}'.")
        
        reason_clean = reason.strip()
        tx.write({
            'state': 'cancel',
            'bestpay_cancel_reason': reason_clean,
            'state_message': f'Cancelada por el cliente vía API. Motivo: {reason_clean}',
        })
        
        return {
            'status': 'success',
            'message': 'Transacción cancelada correctamente.',
            'transaction_status': 'cancel',
            'transaction': self._serialize_transaction(tx),
        }
