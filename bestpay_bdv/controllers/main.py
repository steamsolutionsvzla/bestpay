# -*- coding: utf-8 -*-
import json
import logging
import re
import secrets
import time
from odoo import http, fields
from odoo.http import request, Response
from odoo.exceptions import UserError

from odoo.addons.bestpay.utils.api_logger import api_call_logger
from odoo.addons.bestpay.utils.business_logger import is_logged, log_business_error

_logger = logging.getLogger(__name__)


class BestpayBDVController(http.Controller):

    def _bestpay_c2p_unavailable_reason(self, transaction):
        """Motivo por el que C2P no puede operar sobre la transacción, o False si está vigente.

        C2P debita dinero real de la cuenta del cliente: nunca debe ejecutarse sobre
        una transacción cerrada (pagada, cancelada, con error o vencida) ni dos veces.
        """
        if transaction.state == 'done':
            return 'Esta transacción ya fue pagada exitosamente.'
        if transaction.state in ('cancel', 'error'):
            return 'Esta transacción ya no está disponible. Solicita un nuevo link de pago.'
        if transaction.state == 'no_pagado':
            return 'Este link de pago venció. Solicita un nuevo link de pago.'
        if transaction.state in ('draft', 'pending') and transaction._bestpay_is_link_expired():
            transaction._bestpay_mark_as_unpaid()
            return 'Este link de pago venció. Solicita un nuevo link de pago.'
        if transaction.bdv_c2p_end_to_end_id:
            return 'Esta transacción ya fue procesada.'
        return False

    def _bestpay_link_is_unavailable(self, transaction):
        """Un link no es pagable si ya venció o si la transacción no está vigente.

        Cubre el estado 'no_pagado' y también el caso en que el cron todavía
        no ha marcado la transacción pero ya pasaron las 48h.
        """
        if transaction.state == 'no_pagado':
            return True
        return (
            transaction.state in ('draft', 'pending')
            and transaction._bestpay_is_link_expired()
        )

    def _bestpay_render_result(self, transaction, status, message):
        """Renderiza la pantalla de resultado e inyecta los botones de salida.

        - success: muestra "Volver al portal" y redirige automáticamente.
        - rejected: Pago Móvil puede reintentarse con el mismo link.
        - expired / error: solo muestra el botón de retorno, sin auto.

        Si el tercero no configuró ninguna URL de retorno, la pantalla se
        comporta igual que antes (sin botones ni redirección).
        """
        retry_url = False
        if (
            status in ('rejected', 'invalid')
            and transaction.payment_method_id.code != 'bdv_c2p'
            and transaction.uuid_hash
        ):
            retry_url = f"/pago/bdv/checkout?hash={transaction.uuid_hash}"

        return request.render('bestpay_bdv.bdv_checkout_result', {
            'transaction': transaction,
            'status': status,
            'message': message,
            'return_url': transaction._bdv_get_return_url(status),
            'retry_url': retry_url,
            'return_seconds': 10 if status == 'success' else 0,
        })

    def _bestpay_expired_link_response(self, transaction):
        """Renderiza la pantalla de link vencido y sincroniza el estado si hace falta."""
        if transaction.state in ('draft', 'pending') and transaction._bestpay_is_link_expired():
            transaction._bestpay_mark_as_unpaid()
        return self._bestpay_render_result(
            transaction,
            'expired',
            'Este link de pago venció. Por favor solicita un nuevo link de pago.',
        )

    # =====================================================
    # C2P INTERBANCARIO (clave del cliente)
    # =====================================================
    # El paymentkey del BDV solo envía la clave a clientes del BDV. Para pagadores
    # de otros bancos la clave C2P la genera el cliente en su propia banca, por lo
    # que no se solicita al BDV y se usa directamente en el cobro.
    C2P_CLIENT_KEY_MESSAGE = (
        'Genera tu clave C2P de 8 dígitos en la aplicación o banca en línea de tu banco '
        'e ingrésala aquí para autorizar el cobro.'
    )

    # Códigos de la tabla de errores del BDV con un mensaje accionable.
    C2P_BANK_ERRORS = {
        '1002': 'El banco del cliente no respondió a tiempo. Intenta nuevamente.',
        '1014': 'El cliente no está afiliado a Pago Móvil en el banco seleccionado.',
        '1015': 'El número no está afiliado a la Clave Móvil del banco seleccionado.',
        '1034': 'Saldo insuficiente en la cuenta del cliente.',
        '1050': 'El banco tardó demasiado en responder. Intenta nuevamente.',
        '1055': 'La clave C2P no es válida o expiró. Genera una nueva en tu banco e inténtalo otra vez.',
        '1056': 'El número de teléfono no corresponde con el titular de la cuenta.',
        '1061': 'El monto supera el límite diario permitido por el banco.',
        '1062': 'La cuenta del cliente presenta inconvenientes. Debe comunicarse con su banco.',
        '1091': 'El banco seleccionado no está disponible para C2P en este momento.',
        '1092': 'El banco seleccionado no se encuentra afiliado a C2P.',
    }

    def _bestpay_c2p_client_key_message(self):
        """Instrucción genérica para que el pagador genere su clave C2P."""
        return self.C2P_CLIENT_KEY_MESSAGE

    def _bestpay_c2p_bank_error(self, code, bank_message):
        """Traduce el código de error del BDV a un mensaje claro para el cliente."""
        bank_message = bank_message or ''
        # El banco devuelve este texto cuando la clave no tiene 8 dígitos.
        if re.search(r'digit', bank_message, re.IGNORECASE):
            return 'La clave C2P debe tener 8 dígitos.'
        code = str(code or '').strip()
        mapped = self.C2P_BANK_ERRORS.get(code)
        if mapped:
            return f"{mapped} (código {code})"
        return bank_message or 'El banco rechazó el pago.'

    # =====================================================
    # 2. WEB: FORMULARIO DE PAGO (Para el usuario final)
    # =====================================================
    @http.route('/pago/bdv/checkout', type='http', auth='none', website=False)
    @api_call_logger
    def bdv_checkout(self, hash=None, **kw):
        if not hash:
            return request.not_found()
        
        transaction = request.env['payment.transaction'].sudo().search([
            ('uuid_hash', '=', hash)
        ], limit=1)
        
        if not transaction.exists():
            return request.not_found()
            
        if transaction.state == 'done':
            return self._bestpay_render_result(
                transaction,
                'success',
                'Este pago ya fue procesado exitosamente.',
            )

        if transaction.state == 'error':
            return self._bestpay_render_result(
                transaction,
                'error',
                'Este link de pago ya no está disponible. Por favor solicita un nuevo link de pago.',
            )

        if transaction.state == 'cancel' and transaction.bdv_conciliation_state != 'rejected':
            return self._bestpay_render_result(
                transaction,
                'error',
                'Este link de pago ya no está disponible. Por favor solicita un nuevo link de pago.',
            )

        # Link vencido ('no_pagado' o más de 48h desde su creación): no se puede pagar.
        if self._bestpay_link_is_unavailable(transaction):
            return self._bestpay_expired_link_response(transaction)

        # Antes de mostrar el monto al usuario, refrescamos con la tasa BCV del día
        transaction._bestpay_action_recalculate_jit_ves()

        # --- NUEVA LÓGICA DE ENRUTAMIENTO ---
        method_code = transaction.payment_method_id.code

        if method_code == 'bdv_c2p':
            # Renderiza el NUEVO formulario multi-paso para C2P
            return request.render('bestpay_bdv.c2p_checkout_form', {
                'transaction': transaction,
                'hash': hash,
            })
        else:
            # --- 🎨 DATOS DEL COMERCIO PARA EL CHECKOUT ---
            # 1. El QR pertenece al COMERCIO receptor, no al cliente pagador.
            # Se busca en la compañía del provider con sudo() para evitar bloqueos por ir.rule
            merchant = (
                transaction.provider_id.company_id.partner_id.sudo()
                if transaction.provider_id and transaction.provider_id.company_id
                else transaction.partner_id.sudo()
            )

            # Si el merchant principal no tiene QR configurado, evaluar si está en el partner de la transacción
            if not merchant.bestpay_pagomovil_qr and transaction.partner_id.sudo().bestpay_pagomovil_qr:
                merchant = transaction.partner_id.sudo()

            _logger.warning(
                f"🔍 DEBUG QR: TX={transaction.id} | Merchant ID={merchant.id} | "
                f"Nombre={merchant.name} | ¿Tiene QR?: {bool(merchant.bestpay_pagomovil_qr)}"
            )

# 2. Manejo seguro de la imagen QR (evitar doble encode de base64)
            qr_url = False
            raw_qr = merchant.bestpay_pagomovil_qr
            if raw_qr:
                import base64
                try:
                    # En Odoo fields.Binary entrega string o bytes ya en base64
                    if isinstance(raw_qr, bytes):
                        raw_str = raw_qr.decode('utf-8').strip()
                    else:
                        raw_str = str(raw_qr).strip()

                    # Verificar si ya es un base64 válido
                    # Si no lo es, se captura en la excepción y se codifica
                    base64.b64decode(raw_str, validate=True)
                    qr_b64 = raw_str
                except Exception:
                    # Si eran bytes crudos de imagen, se codifican a base64
                    qr_b64 = base64.b64encode(raw_qr if isinstance(raw_qr, bytes) else raw_qr.encode()).decode('utf-8')

                qr_url = f"data:image/png;base64,{qr_b64}"

            comercio_data = {
                'nombre': merchant.bestpay_checkout_title or merchant.name or 'Comercio',
                'rif': merchant.vat or '',
                'logo_url': f"/web/image/res.partner/{merchant.id}/image_1920" if merchant.image_1920 else False,
                'telefono_destino': getattr(merchant, 'bdv_telefono_destino', '') or '',
                'mensaje': merchant.bestpay_checkout_message or '',
                'color': merchant.bestpay_checkout_primary_color or '#0033a0',
                'pagomovil_telefono': merchant.bestpay_pagomovil_telefono or '',
                'pagomovil_rif': merchant.bestpay_pagomovil_rif or '',
                'pagomovil_banco': merchant.bestpay_pagomovil_banco or '',
                'pagomovil_qr_url': qr_url,
            }
            
            # Renderiza el formulario de Pago Móvil
            return request.render('bestpay_bdv.bdv_checkout_form', {
                'transaction': transaction,
                'hash': hash,
                'comercio': comercio_data,
            })

    # =====================================================
    # 3. WEB: PROCESAR EL PAGO (Envío al BDV)
    # =====================================================
    @http.route('/pago/bdv/procesar', type='http', auth='none', methods=['POST'], csrf=False)
    @api_call_logger
    def bdv_process_payment(self, **post):
        tx_hash = post.get('hash')
        if not tx_hash:
            return request.not_found()
            
        transaction = request.env['payment.transaction'].sudo().search([
            ('uuid_hash', '=', tx_hash)
        ], limit=1)
        
        if not transaction.exists():
            return request.not_found()

        if transaction.state == 'done':
            return self._bestpay_render_result(
                transaction,
                'success',
                'Este pago ya fue procesado exitosamente.',
            )

        if transaction.state == 'error':
            return self._bestpay_render_result(
                transaction,
                'error',
                'Este link de pago ya no está disponible. Por favor solicita un nuevo link de pago.',
            )

        if transaction.state == 'cancel' and transaction.bdv_conciliation_state != 'rejected':
            return self._bestpay_render_result(
                transaction,
                'error',
                'Este link de pago ya no está disponible. Por favor solicita un nuevo link de pago.',
            )

        # Bloqueo del POST: evita pagar con el HTML ya abierto de un link vencido.
        if self._bestpay_link_is_unavailable(transaction):
            return self._bestpay_expired_link_response(transaction)

        # Mapear datos del formulario a los campos del modelo
        cedula_raw = (post.get('cedula') or '').strip()
        tipo_cedula = post.get('tipo_cedula', 'V')
        cedula = f"{tipo_cedula}{cedula_raw}"

        telefono_raw = (post.get('telefono') or '').strip()
        if telefono_raw and not telefono_raw.startswith('0'):
            telefono_pagador = f"0{telefono_raw}"
        else:
            telefono_pagador = telefono_raw

        # Validaciones según la documentación de la API de Conciliación:
        # teléfono de 10 dígitos (sin 0) o 11 con el 0, cédula 6-10 dígitos y
        # referencia de 4 a 12 dígitos. Evita enviar al banco datos con formato
        # inválido (HTTP 400 / 1010 "Campo ... invalido").
        # Son errores de datos, no un pago rechazado por el banco: se muestra
        # la pantalla "Revisa los datos", no "Pago Rechazado".
        if not re.fullmatch(r'0\d{10}', telefono_pagador or ''):
            return self._bestpay_render_result(
                transaction,
                'invalid',
                'El teléfono del pagador debe tener 10 dígitos sin el 0 inicial (ej: 4121234567).',
            )

        if not re.fullmatch(r'\d{6,10}', cedula_raw):
            return self._bestpay_render_result(
                transaction,
                'invalid',
                'La cédula del pagador no es válida (debe tener entre 6 y 10 dígitos).',
            )

        referencia_raw = (post.get('referencia') or '').strip()
        if not re.fullmatch(r'\d{4,12}', referencia_raw):
            return self._bestpay_render_result(
                transaction,
                'invalid',
                'El número de referencia debe tener entre 4 y 12 dígitos, como aparece en tu comprobante.',
            )

        # =====================================================
        # LÓGICA DE MONTO: QA (Hardcodeado) vs PRODUCCIÓN (Real)
        # =====================================================
        # Leemos el entorno directamente del provider de la transacción
        env_type = transaction.provider_id.bdv_environment.strip().lower() if transaction.provider_id.bdv_environment else 'qa'
        
        transaction._bestpay_action_recalculate_jit_ves()
        
        if env_type == 'qa':
            # AMBIENTE DE CALIDAD: Monto fijo solicitado por el banco
            importe_float = 120.00
            _logger.info(f"[BDV] 🟢 Ambiente QA detectado (desde provider). Forzando monto a enviar: {importe_float} Bs")
        else:
            # AMBIENTE DE PRODUCCIÓN: Tomar el monto real que viene del e-commerce
            # 1. Intentamos usar amount_ves si existe y es válido
            importe_float = getattr(transaction, 'amount_ves', 0.0)
            
            # 2. Si amount_ves es 0, None o no existe, usamos el amount base de la transacción
            if not importe_float or float(importe_float) <= 0:
                importe_float = float(transaction.amount)
                
            _logger.info(f"[BDV] 🔵 Ambiente PRODUCCIÓN detectado (desde provider). Monto a enviar al banco: {importe_float} Bs (ID Transacción: {transaction.id})")
        
        # Formateamos a 2 decimales como string para el payload del banco
        importe_str = f"{importe_float:.2f}"
        # =====================================================
        # =====================================================

        # =====================================================
        # PROCESAR FECHA DEL PAGO
        # =====================================================
        fecha_pago_str = post.get('fecha_pago', '')
        try:
            from datetime import datetime
            # Convertir string a date object
            fecha_pago = datetime.strptime(fecha_pago_str, '%Y-%m-%d').date()
            
            hoy = fields.Date.today()
            dias_diferencia = (hoy - fecha_pago).days
            
            if dias_diferencia < 0:
                return self._bestpay_render_result(
                    transaction,
                    'invalid',
                    'La fecha del pago no puede ser futura.',
                )
            
            # La regla estricta de 3 días SOLO aplica en PRODUCCIÓN.
            # En QA, el banco exige usar fechas específicas de prueba (ej: 02/12/2023).
            if env_type != 'qa' and dias_diferencia > 3:
                return self._bestpay_render_result(
                    transaction,
                    'invalid',
                    'El BDV solo permite conciliar pagos de los últimos 3 días. '
                    f"Fecha del pago: {fecha_pago.strftime('%d/%m/%Y')}. "
                    f"Hoy es: {hoy.strftime('%d/%m/%Y')}.",
                )
            
            _logger.info(f"[BDV] Fecha del pago validada: {fecha_pago} ({dias_diferencia} días atrás) - Ambiente: {env_type.upper()}")
            
        except ValueError:
            return self._bestpay_render_result(
                transaction,
                'invalid',
                'Indica una fecha de pago válida.',
            )
        except Exception as e:
            _logger.error(f"[BDV] Error procesando fecha: {str(e)}")
            return self._bestpay_render_result(
                transaction,
                'invalid',
                'No se pudo leer la fecha del pago. Verifícala e intenta de nuevo.',
            )
        # =====================================================

        # Actualizar la transacción con los datos del pagador y el monto determinado
        transaction.write({
            'bdv_cedula_pagador': cedula,
            'bdv_telefono_pagador': telefono_pagador,
            'bdv_banco_origen': post.get('banco', '0102'),
            'bdv_referencia': referencia_raw,
            'bdv_fecha_pago': fecha_pago,  # ← AHORA USA LA FECHA DEL FORMULARIO
            'bdv_importe': importe_float,
        })

        # =====================================================
        # SELECCIONAR TELÉFONO DESTINO SEGÚN AMBIENTE
        # =====================================================
        env_type = transaction.provider_id.bdv_environment.strip().lower() if transaction.provider_id.bdv_environment else 'qa'

        telefono_destino = (
            transaction.partner_id.bdv_telefono_destino_qa 
            if env_type == 'qa' and transaction.partner_id.bdv_telefono_destino_qa
            else transaction.partner_id.bdv_telefono_destino
        )
        # =====================================================

        # Llamar al método de conciliación (el que creamos en el modelo)
        try:
            is_approved = transaction.bdv_send_conciliation()
            
            if is_approved:
                return self._bestpay_render_result(
                    transaction,
                    'success',
                    '¡Pago aprobado exitosamente por el Banco de Venezuela!',
                )
            else:
                mensaje = (
                    transaction.bdv_conciliation_message
                    or 'No se pudo validar el pago. Revisa los datos e intenta nuevamente.'
                )
                # Si el problema son los datos (campo inválido, monto distinto o
                # pago no encontrado), mostrar "Revisa los datos" en vez de la
                # alarmante pantalla "Pago Rechazado".
                if mensaje == 'La API Key del comercio no está afiliada al servicio de conciliación del BDV.':
                    return self._bestpay_render_result(transaction, 'error', mensaje)

                datos_invalidos = set(transaction._BDV_CONCILIATION_FIELD_ERRORS.values())
                datos_invalidos.update({
                    'El monto reportado no coincide con el pago realizado.',
                    'No se encontró un pago con esos datos. Verifica referencia, '
                    'teléfono, cédula, monto y fecha.',
                })
                status = 'invalid' if mensaje in datos_invalidos else 'rejected'
                return self._bestpay_render_result(transaction, status, mensaje)

        except UserError as e:
            _logger.error(f"[BDV] Error de usuario: {str(e)}")
            return self._bestpay_render_result(transaction, 'error', str(e))
        except Exception as e:
            _logger.error(f"[BDV] Error inesperado: {str(e)}")
            if not is_logged(e):
                log_business_error(
                    request.env,
                    'bdv_process_payment',
                    input_data={'tx': transaction.reference, 'hash': tx_hash},
                    output_data='Error inesperado procesando el pago BDV: %s' % e,
                    exc=e,
                    is_error=True,
                    http=True,
                    meta={'proveedor': 'bdv', 'tx': transaction.reference},
                )
            return self._bestpay_render_result(
                transaction,
                'error',
                'Ocurrió un error inesperado al procesar el pago.',
            )
    

    # =====================================================
    # 4. WEBHOOK: NOTIFICACIÓN DEL BANCO (Server-to-Server)
    # =====================================================
    @http.route('/api/bestpay/v1/webhook/bdv', type='http', auth='none', methods=['POST', 'OPTIONS'], csrf=False)
    @api_call_logger
    def bdv_webhook_notify(self, **post):
        cors_headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'API-Key, A-Key, Content-Type, Accept',
        }
        if request.httprequest.method == 'OPTIONS':
            return Response('', status=200, headers=cors_headers)
        try:
            raw_data = request.httprequest.data.decode('utf-8')
            _logger.warning(f"[BDV NOTIFICACIÓN] Webhook recibido. Body: {raw_data}")
            
            # 1. Validar API-KEY del header
            headers = request.httprequest.headers
            api_key_header = (
                headers.get('A-Key', '') or 
                headers.get('API-KEY', '') or 
                headers.get('Api-Key', '') or 
                headers.get('api-key', '')
            ).strip()
            _logger.warning(f"[BDV NOTIFICACIÓN] Headers recibidos: {list(headers.keys())}")
            _logger.warning(f"[BDV NOTIFICACIÓN] API-Key extraída: '{api_key_header}'")

            if not api_key_header:
                _logger.warning(f"[BDV NOTIFICACIÓN] ❌ No se encontró API-Key en headers")
                return Response(
                    json.dumps({"codigo": "99", "mensajeCliente": "Corrija el API KEY", "mensajeSistema": "Error en API KEY"}),
                    status=200, content_type='application/json', headers=cors_headers
                )

            # 2. Parsear JSON
            try:
                payload = json.loads(raw_data)
            except json.JSONDecodeError:
                _logger.warning(f"[BDV NOTIFICACIÓN]  JSON inválido")
                return Response(
                    json.dumps({"codigo": "99", "mensajeCliente": "JSON inválido", "mensajeSistema": "Error al parsear"}),
                    status=200, content_type='application/json', headers=cors_headers
                )

            # 3. Identificar el comercio receptor
            numero_comercio = payload.get('numeroComercio', '')
            _logger.warning(f"[BDV NOTIFICACIÓN] numeroComercio recibido: '{numero_comercio}'")

            # 4. Buscar el Partner
            partner = request.env['res.partner'].sudo().search([
                '|',
                ('bdv_telefono_destino', '=', numero_comercio),
                ('bdv_telefono_notificacion', '=', numero_comercio)
            ], limit=1)
            
            if partner:
                _logger.warning(f"[BDV NOTIFICACIÓN] ✅ Partner encontrado: {partner.name} (ID: {partner.id})")
                _logger.warning(f"[BDV NOTIFICACIÓN] bdv_api_key_notification: '{partner.bdv_api_key_notification}'")
                _logger.warning(f"[BDV NOTIFICACIÓN] bdv_api_key_prod: '{partner.bdv_api_key_prod}'")
            else:
                _logger.warning(f"[BDV NOTIFICACIÓN] ❌ NO se encontró ningún partner con el teléfono: '{numero_comercio}'")
                # Puede ser un comercio nuevo sin configurar o un intento
                # externo. Se registra para diagnóstico, sin alertar al grupo.
                log_business_error(
                    request.env,
                    'bdv_webhook_notify',
                    input_data={'numero_comercio': numero_comercio},
                    output_data=(
                        'No se encontró un comercio con el teléfono de '
                        'notificación %s.' % numero_comercio
                    ),
                    is_error=False,
                    http=True,
                    meta={'proveedor': 'bdv'},
                )

            # 5. Validar API Key según el entorno del proveedor (QA vs Producción)
            api_key_valida = False
            if partner:
                # Buscar el provider BDV para saber en qué entorno estamos
                provider = request.env['payment.provider'].sudo().search([
                    ('code', '=', 'bdv'),
                    ('is_bestpay_provider', '=', True),
                ], limit=1)
                
                env_type = provider.bdv_environment if provider else 'qa'
                _logger.warning(f"[BDV NOTIFICACIÓN] Entorno detectado en Provider: {env_type}")

                if env_type == 'prod':
                    # En Producción, el BDV usa una sola API Key para todo
                    api_key_configurada = partner.bdv_api_key_prod or ''
                    _logger.warning(f"[BDV NOTIFICACIÓN] Usando API Key de Producción (bdv_api_key_prod)")
                else:
                    # En QA, usamos la key específica de notificación o el fallback
                    api_key_configurada = partner.bdv_api_key_notification or '97F6F54EF1A84F3A24FE19A3B338C77A'
                    _logger.warning(f"[BDV NOTIFICACIÓN] Usando API Key de QA (bdv_api_key_notification)")

                if api_key_configurada:
                    api_key_valida = (api_key_header == api_key_configurada)
                    if api_key_valida:
                        _logger.warning(f"[BDV NOTIFICACIÓN] ✅ API Key validada correctamente")
                    else:
                        _logger.warning(f"[BDV NOTIFICACIÓN] ⚠️ API Key NO coincide. Recibida: '{api_key_header}', Configurada: '{api_key_configurada}'")
                else:
                    _logger.warning(f"[BDV NOTIFICACIÓN] ❌ El partner no tiene API Key configurada para el entorno {env_type}")
            else:
                # Respaldo para QA si no se encontró partner
                api_key_valida = (api_key_header == '97F6F54EF1A84F3A24FE19A3B338C77A')
                if not api_key_valida:
                    _logger.warning(f"[BDV NOTIFICACIÓN] ️ API Key NO coincide con el fallback de QA")

            if not api_key_valida:
                _logger.warning(f"[BDV NOTIFICACIÓN]  Bloqueado por API Key inválida")
                return Response(
                    json.dumps({"codigo": "99", "mensajeCliente": "Corrija el API KEY", "mensajeSistema": "Error en API KEY"}),
                    status=200, content_type='application/json', headers=cors_headers
                )

            _logger.warning(f"[BDV NOTIFICACIÓN] ✅ API Key validada correctamente. Pasando al modelo...")

            # ==========================================
            # 📝 GUARDAR O ACTUALIZAR NOTIFICACIÓN (IDEMPOTENCIA)
            # ==========================================
            existing_notification = False # Variable de control
            try:
                external_ref = payload.get('referenciaBancoOrdenante', '')
                tel_crudo = str(payload.get('numeroCliente', '')).strip()
                if tel_crudo and not tel_crudo.startswith('0'):
                    tel_crudo = '0' + tel_crudo

                # Buscar si YA existe una notificación con esta referencia
                existing_notification = request.env['bestpay.bank.notification'].sudo().search([
                    ('external_ref', '=', external_ref),
                    ('provider_code', '=', 'bdv'),
                ], limit=1)

                if existing_notification:
                    # Ya existe → NO actualizar received_at (debe mantener la fecha original)
                    # Solo logueamos que llegó una re-notificación
                    _logger.info(f"[BDV] Re-notificación detectada. Referencia: {external_ref}. No se crea duplicado. Fecha original mantenida: {existing_notification.received_at}")
                else:
                    # No existe → Crear nueva
                    request.env['bestpay.bank.notification'].sudo().create({
                        'provider_code': 'bdv',
                        'partner_id': partner.id if partner else False,
                        'payer_phone': tel_crudo,
                        'external_ref': external_ref,
                        'amount': float(payload.get('monto', 0) or 0),
                        'status': 'received',
                        'raw_payload': json.dumps(payload, ensure_ascii=False, indent=2),
                    })
                    _logger.info(f"[BDV] Nueva notificación creada. Referencia: {external_ref}")
                    
            except Exception as e:
                _logger.warning(f"[BDV] No se pudo guardar/actualizar Notificación: {e}")
            # ==========================================

            # 6. Delegar la lógica de negocio al modelo
            resultado = request.env['payment.transaction'].sudo().bdv_process_notification_webhook(payload, partner)

            # 7. Responder al BDV (IDEMPOTENCIA)
            if existing_notification or resultado.get('codigo') == '01':
                # Si ya existía en BD o el modelo nos dijo que la TX ya estaba pagada
                return Response(
                    json.dumps({"codigo": "01", "mensajeCliente": "pago previamente recibido", "mensajeSistema": "renotificado"}),
                    status=200, content_type='application/json', headers=cors_headers
                )
            else:
                # Primera vez y procesada correctamente
                return Response(
                    json.dumps({"codigo": "00", "mensajeCliente": "Aprobado", "mensajeSistema": "Notificado"}),
                    status=200, content_type='application/json', headers=cors_headers
                )
        except Exception as e:
            _logger.error(f"[BDV NOTIFICACIÓN] Error inesperado: {e}", exc_info=True)
            if not is_logged(e):
                log_business_error(
                    request.env,
                    'bdv_webhook_notify',
                    output_data='Error inesperado procesando el webhook del BDV: %s' % e,
                    exc=e,
                    is_error=True,
                    http=True,
                    meta={'proveedor': 'bdv'},
                )
            return Response(
                json.dumps({"codigo": "00", "mensajeCliente": "Error interno", "mensajeSistema": "Notificación recibida con error"}),
                status=200, content_type='application/json', headers=cors_headers
            )

    # =====================================================
    # MÉTODO AUXILIAR: GENERAR REFERENCIA SECUENCIAL
    # =====================================================
    def _generate_bestpay_reference(self, external_ref):
        """
        Genera una referencia única usando ir.sequence de Odoo.
        Formato: BP-{AÑO}-{EXTERNA}-{SECUENCIAL}
        """
        from datetime import datetime
        year = datetime.now().year
        ext_clean = (external_ref or 'NOREF').replace(' ', '-').upper()
        
        # Intentar obtener el siguiente número de la secuencia
        seq_number = request.env['ir.sequence'].sudo().next_by_code('bestpay.transaction.reference')
        
        # Si la secuencia no existe o devuelve False, crearla al vuelo
        if not seq_number:
            _logger.warning("[API] Secuencia no encontrada, creando automáticamente...")
            seq = request.env['ir.sequence'].sudo().create({
                'name': 'BestPay Transaction Reference',
                'code': 'bestpay.transaction.reference',
                'prefix': f'BP-{year}-',
                'padding': 7,
                'number_next': 1,
                'number_increment': 1,
            })
            seq_number = seq.next_by_code('bestpay.transaction.reference')
        
        # Si aún así falla, usar timestamp como fallback
        if not seq_number:
            import time
            seq_number = str(int(time.time()))
            _logger.error(f"[API] Fallback a timestamp: {seq_number}")
        
        return f"BP-{year}-{ext_clean}-{seq_number}"       

     # =====================================================
    # 5. API C2P: GENERAR OTP (Paso 1 del flujo multi-paso)
    # =====================================================
    @http.route('/api/bestpay/v1/c2p/generate_otp', type='jsonrpc', auth='none', methods=['POST'], csrf=False)
    @api_call_logger
    def c2p_generate_otp(self, uuid_hash=None, customer_document_id=None, customer_phone=None, customer_bank_code=None, **kw):
        """
        Recibe los datos iniciales del cliente y solicita el OTP al BDV.
        """
        _logger.info(f"🔍 DEBUG C2P OTP: uuid_hash recibido = '{uuid_hash}' (tipo: {type(uuid_hash)})")
        
        if not uuid_hash:
            return {'success': False, 'error': 'Falta el identificador de la transacción (uuid_hash).'}

        # --- NORMALIZACIÓN DEL TELÉFONO (Red de seguridad) ---
        if customer_phone:
            customer_phone = customer_phone.strip()
            if not customer_phone.startswith('0'):
                customer_phone = '0' + customer_phone
        # -----------------------------------------------------

        transaction = request.env['payment.transaction'].sudo().search([('uuid_hash', '=', uuid_hash)], limit=1)
        
        _logger.info(f"🔍 DEBUG C2P OTP: Transacción encontrada = {transaction.exists()}")
        if transaction.exists():
            _logger.info(f"🔍 DEBUG C2P OTP: Código del proveedor = '{transaction.provider_id.code}'")
            
        if not transaction.exists() or transaction.provider_id.code != 'bdv':
            return {'success': False, 'error': 'Transacción no válida.'}

        # Guardas de estado: no se pide OTP para transacciones cerradas o vencidas.
        unavailable_reason = self._bestpay_c2p_unavailable_reason(transaction)
        if unavailable_reason:
            _logger.info(
                "[BDV C2P OTP] Bloqueado para Tx %s (estado: %s): %s",
                transaction.reference, transaction.state, unavailable_reason
            )
            return {'success': False, 'error': unavailable_reason}

        try:
            # 1. Guardamos los datos iniciales en la transacción
            bank_code = (customer_bank_code or '0102').strip()
            transaction.write({
                'bdv_c2p_customer_document_id': customer_document_id,
                'bdv_c2p_customer_phone': customer_phone,
                'bdv_c2p_customer_bank_code': bank_code,
                'bdv_c2p_status': 'processing'
            })

            # 2. C2P interbancario: la clave la genera el cliente en su banco.
            # Solo se pide la clave al BDV cuando el pagador es cliente BDV (0102);
            # para otros bancos el paymentkey del BDV envía una clave BDV que el
            # banco del pagador rechaza con el código 1055.
            if bank_code != '0102':
                transaction.write({'bdv_c2p_status': 'otp_sent'})
                transaction._bdv_c2p_log_stage(
                    "GENERATE OTP · CLAVE DEL CLIENTE (sin paymentkey BDV)",
                    request_payload={
                        'customerDocumentId': customer_document_id,
                        'customerNumberInstrument': customer_phone,
                        'customerBankCode': bank_code,
                    },
                )
                _logger.info(
                    "[BDV C2P OTP] Banco %s: no se solicita clave al BDV; "
                    "el cliente genera su clave C2P en su banco.", bank_code
                )
                return {
                    'success': True,
                    'otp_sent': False,
                    'message': self._bestpay_c2p_client_key_message(),
                    'status': 'otp_sent',
                    'bank_code': bank_code,
                }

            # 3. Cliente BDV: el banco envía la clave al teléfono.
            result = transaction.bdv_c2p_generate_otp()

            return {
                'success': True,
                'otp_sent': True,
                'message': result.get('message', 'OTP enviado correctamente. Revise su teléfono.'),
                'status': transaction.bdv_c2p_status,
                'bank_code': bank_code,
            }

        except UserError as e:
            return {'success': False, 'error': str(e)}
        except Exception as e:
            _logger.error(f"[BDV C2P OTP] Error inesperado: {str(e)}", exc_info=True)
            if not is_logged(e):
                log_business_error(
                    request.env,
                    'c2p_generate_otp',
                    input_data={'tx': transaction.reference},
                    output_data='Error inesperado generando el OTP C2P: %s' % e,
                    exc=e,
                    is_error=True,
                    http=True,
                    meta={'proveedor': 'bdv', 'tx': transaction.reference},
                )
            return {'success': False, 'error': 'Error interno del servidor al generar OTP.'}

    # =====================================================
    # 6. API C2P: PROCESAR PAGO (Paso 2 del flujo multi-paso)
    # =====================================================
    @http.route('/api/bestpay/v1/c2p/process_payment', type='jsonrpc', auth='none', methods=['POST'], csrf=False)
    @api_call_logger
    def c2p_process_payment(self, uuid_hash=None, otp=None, **kw):
        """
        Recibe el OTP del cliente y ejecuta el cobro real en el BDV.
        """
        if not uuid_hash or not otp:
            return {'success': False, 'error': 'Faltan datos obligatorios (uuid_hash u OTP).'}

        otp_clean = str(otp).strip()
        # La clave C2P de otros bancos puede ser alfanumérica; la validación
        # definitiva la hace el banco. Solo se filtra longitud y caracteres.
        if not re.fullmatch(r'[A-Za-z0-9]{4,10}', otp_clean):
            return {'success': False, 'error': 'El código ingresado no es válido.'}

        transaction = request.env['payment.transaction'].sudo().search([('uuid_hash', '=', uuid_hash)], limit=1)
        if not transaction.exists() or transaction.provider_id.code != 'bdv':
            return {'success': False, 'error': 'Transacción no válida.'}

        # Guardas de estado: nunca cobrar dos veces ni sobre links cerrados/vencidos.
        unavailable_reason = self._bestpay_c2p_unavailable_reason(transaction)
        if unavailable_reason:
            _logger.info(
                "[BDV C2P Process] Bloqueado para Tx %s (estado: %s): %s",
                transaction.reference, transaction.state, unavailable_reason
            )
            # Si el cobro ya se completó (doble envío o página desactualizada),
            # el navegador debe ir al comprobante en vez de mostrar un error.
            if transaction.state == 'done':
                return {
                    'success': True,
                    'message': 'Este pago ya fue procesado exitosamente.',
                    'redirect_url': f"/pago/bdv/checkout?hash={uuid_hash}",
                    'end_to_end_id': transaction.bdv_c2p_end_to_end_id,
                }
            return {'success': False, 'error': unavailable_reason}

        # El cobro solo se permite si el OTP fue solicitado previamente.
        if transaction.bdv_c2p_status in (False, 'pending'):
            return {
                'success': False,
                'error': 'Primero debes solicitar el código OTP para esta transacción.'
            }

        # Clave C2P interbancaria: el banco exige exactamente 8 dígitos.
        # Se valida aquí para no gastar una llamada al banco con una clave inválida.
        if (transaction.bdv_c2p_customer_bank_code or '0102') != '0102' and not re.fullmatch(r'\d{8}', otp_clean):
            return {
                'success': False,
                'error': 'La clave C2P debe tener 8 dígitos.',
            }

        try:
            # 1. Guardamos el OTP en la transacción
            transaction.write({
                'bdv_c2p_otp': otp_clean,
                'bdv_c2p_status': 'processing'
            })

            # 2. Llamamos al método del modelo que ejecuta el cobro
            result = transaction.bdv_c2p_process_payment()
            
            if result.get('success'):
                # Redirección relativa: no depende de que web.base.url apunte al
                # host público por el que está navegando el cliente.
                redirect_path = f"/pago/bdv/checkout?hash={uuid_hash}"
                base_url = request.env['ir.config_parameter'].sudo().get_param('web.base.url')
                return {
                    'success': True,
                    'message': 'Pago aprobado exitosamente.',
                    'redirect_url': redirect_path,
                    'payment_link': f"{base_url}{redirect_path}",
                    'reference': transaction.bdv_c2p_reference_generated or transaction.reference,
                    'end_to_end_id': transaction.bdv_c2p_end_to_end_id
                }
            else:
                # El banco rechazó el cobro (por ejemplo, OTP incorrecto). No se anula
                # nada porque no se movió dinero; la transacción queda 'pending' para
                # que el cliente pueda reintentar con un OTP válido.
                if transaction.bdv_c2p_end_to_end_id:
                    transaction.bdv_c2p_annul()
                error_code = str(result.get('code') or '')
                return {
                    'success': False,
                    'code': error_code,
                    'error': self._bestpay_c2p_bank_error(error_code, result.get('message')),
                }

        except UserError as e:
            return {'success': False, 'error': str(e)}
        except Exception as e:
            _logger.error(f"[BDV C2P Process] Error inesperado: {str(e)}", exc_info=True)
            # Liberar el flujo para que un fallo técnico no bloquee el reintento.
            try:
                transaction.write({
                    'bdv_c2p_status': 'error',
                    'state_message': 'Error técnico procesando el cobro C2P.',
                })
            except Exception:
                pass
            if not is_logged(e):
                log_business_error(
                    request.env,
                    'c2p_process_payment',
                    input_data={'tx': transaction.reference},
                    output_data='Error inesperado procesando el cobro C2P: %s' % e,
                    exc=e,
                    is_error=True,
                    http=True,
                    meta={'proveedor': 'bdv', 'tx': transaction.reference},
                )
            return {'success': False, 'error': 'Error interno del servidor al procesar el pago.'}

    # =====================================================
    # 6.1 API C2P: CONSULTA DE ESTADO (Reconciliación del checkout)
    # =====================================================
    @http.route('/api/bestpay/v1/c2p/status', type='jsonrpc', auth='none', methods=['POST'], csrf=False)
    @api_call_logger
    def c2p_status(self, uuid_hash=None, **kw):
        """
        Consulta de solo lectura del estado del cobro C2P.

        El checkout la usa para reconciliar cuando la respuesta del banco no
        llegó (timeout, corte de red, proxy) o cuando el cliente reenvía el OTP.
        No ejecuta auto-match de notificaciones ni ningún otro efecto lateral.
        """
        if not uuid_hash:
            return {'status': 'error', 'message': 'Falta el identificador de la transacción.'}

        transaction = request.env['payment.transaction'].sudo().search(
            [('uuid_hash', '=', uuid_hash)], limit=1
        )
        if not transaction.exists() or transaction.provider_id.code != 'bdv':
            return {'status': 'error', 'message': 'Transacción no válida.'}

        # Lectura fresca: el cobro pudo confirmarse en otro worker/proceso.
        transaction.invalidate_recordset([
            'state', 'bdv_c2p_status',
            'bdv_c2p_end_to_end_id', 'bdv_c2p_reference_generated',
        ])

        if transaction.state == 'done':
            return {
                'status': 'done',
                'message': 'Pago completado con éxito.',
                'redirect_url': f"/pago/bdv/checkout?hash={uuid_hash}",
                'end_to_end_id': transaction.bdv_c2p_end_to_end_id,
            }

        return {
            'status': 'pending' if transaction.state in ('draft', 'pending') else transaction.state,
            'message': 'El pago aún no ha sido confirmado.',
            'c2p_status': transaction.bdv_c2p_status,
        }

    # =====================================================
    # 7. API CONSULTA DE MOVIMIENTOS BDV (Proxy para E-commerce)
    # =====================================================
    @http.route('/api/bestpay/v1/bdv/consulta/movimientos', type='jsonrpc', auth='none', methods=['POST'], csrf=False)
    @api_call_logger
    def bdv_consultar_movimientos_api(self, **kwargs):
        """
        Endpoint REST para que el e-commerce consulte sus movimientos bancarios.
        """
        # 1. Validar credenciales M2M (Basic Auth)
        auth_header = request.httprequest.headers.get('Authorization', '')
        if not auth_header.startswith('Basic '):
            return {'status': 'error', 'message': 'Autenticación requerida (Basic Auth).'}
        
        try:
            import base64, binascii
            encoded_token = auth_header[6:].strip()
            decoded_credentials = base64.b64decode(encoded_token, validate=True).decode('utf-8')
            if ':' not in decoded_credentials: raise ValueError('Formato inválido')
        except Exception:
            return {'status': 'error', 'message': 'Credenciales Basic malformadas.'}
        
        if not request.db:
            return {'status': 'error', 'message': 'Base de datos no especificada.'}
        
        # 2. Validar cliente API
        partner = request.env['res.partner'].sudo().search([
            ('is_api_client', '=', True),
            ('bestpay_basic_token', '=', encoded_token),
        ], limit=1)
        if not partner:
            return {'status': 'error', 'message': 'Credenciales inválidas o cliente no autorizado.'}
        
        # 3. Leer y validar parámetros
        cuenta = kwargs.get('cuenta')
        fecha_ini = kwargs.get('fecha_ini')
        fecha_fin = kwargs.get('fecha_fin')
        nro_movimiento = kwargs.get('nro_movimiento', '')
        
        if not all([cuenta, fecha_ini, fecha_fin]):
            return {'status': 'error', 'message': 'Faltan campos obligatorios: cuenta, fecha_ini, fecha_fin.'}
        
        # 4. Buscar proveedor BDV autorizado
        provider = request.env['payment.provider'].sudo().search([
            ('code', '=', 'bdv'), ('is_bestpay_provider', '=', True),
            ('id', 'in', partner.allowed_provider_ids.ids),
        ], limit=1)
        if not provider:
            return {'status': 'error', 'message': 'Proveedor BDV no autorizado para este cliente.'}
        
        # 5. Ejecutar consulta
        try:
            resultado = provider.bdv_consultar_movimientos(
                cuenta=cuenta, fecha_ini=fecha_ini, fecha_fin=fecha_fin, 
                nro_movimiento=nro_movimiento, partner=partner
            )
            if resultado.get('success'):
                return {'status': 'success', 'data': resultado}
            else:
                return {'status': 'error', 'message': resultado.get('message')}
        except Exception as e:
            _logger.error(f"[BDV MOVIMIENTOS API] Error: {str(e)}")
            log_business_error(
                request.env,
                'bdv_consultar_movimientos_api',
                input_data={'partner': partner.name},
                output_data='Error consultando movimientos del BDV: %s' % e,
                exc=e,
                is_error=True,
                http=True,
                meta={'proveedor': 'bdv', 'cliente': partner.name},
            )
            return {'status': 'error', 'message': f'Error interno: {str(e)}'}

    # =====================================================
    # 8. API POLLING: VERIFICAR CASADO AUTOMÁTICO (Checkout)
    # =====================================================
# =====================================================
    # 8. API POLLING: VERIFICAR CASADO AUTOMÁTICO (Checkout)
    # =====================================================
    @http.route('/api/bestpay/v1/bdv/checkout/check_status', type='jsonrpc', auth='none', methods=['POST'], csrf=False)
    @api_call_logger
    def bdv_checkout_check_status(self, hash=None, payer_phone=None, **kw):
        """
        Consulta si existe una notificación bancaria que case con la transacción.
        """
        _logger.info(f"[POLLING LOG] >>> Petición check_status recibida. Hash={hash}, PhoneParam={payer_phone}")

        if not hash:
            _logger.warning("[POLLING LOG] ❌ Error: Hash ausente en la petición.")
            return {'status': 'error', 'message': 'Falta el hash de la transacción.'}

        tx = request.env['payment.transaction'].sudo().search([
            ('uuid_hash', '=', hash)
        ], limit=1)

        if not tx.exists():
            _logger.warning(f"[POLLING LOG] ❌ Error: No existe TX con hash '{hash}'")
            return {'status': 'error', 'message': 'Transacción no encontrada.'}

        # Forzar lectura fresca de PostgreSQL evitando caché en memoria
        tx.invalidate_recordset(['state', 'bdv_referencia', 'amount_ves'])
        _logger.info(f"[POLLING LOG] TX #{tx.id} encontrada | State='{tx.state}' | Ref='{tx.bdv_referencia}' | AmountVES={tx.amount_ves}")

        if tx.state == 'done':
            _logger.info(f"[POLLING LOG] ✅ TX #{tx.id} ESTÁ EN DONE. Respondiendo status='done'")
            return {
                'status': 'done',
                'message': 'Pago completado con éxito.',
                'redirect_url': f"/pago/bdv/checkout?hash={hash}"
            }

        phone_to_match = payer_phone or tx.bdv_telefono_pagador or ''
        if not phone_to_match and tx.payment_request_payload:
            try:
                orig_payload = json.loads(tx.payment_request_payload)
                phone_to_match = orig_payload.get('customer_phone') or orig_payload.get('phone') or ''
            except Exception:
                pass

        phone_to_match = str(phone_to_match).strip()
        if phone_to_match and not phone_to_match.startswith('0'):
            phone_to_match = '0' + phone_to_match

        target_amount = tx.amount_ves if tx.amount_ves > 0 else tx.amount
        _logger.info(f"[POLLING LOG] TX #{tx.id} aún en '{tx.state}'. Evaluando auto-match: Phone={phone_to_match}, Monto={target_amount}")

        matched = tx._bdv_attempt_auto_match_notification(phone=phone_to_match, amount=target_amount)

        if matched:
            _logger.info(f"[POLLING LOG] ✅ Auto-match ejecutado exitosamente para TX #{tx.id}")
            return {
                'status': 'done',
                'message': '¡Pago casado automáticamente!',
                'redirect_url': f"/pago/bdv/checkout?hash={hash}"
            }

        _logger.info(f"[POLLING LOG] ⏳ TX #{tx.id} continúa pendiente. Respondiendo status='pending'")
        return {
            'status': 'pending',
            'message': 'Notificación aún no recibida.'
        }