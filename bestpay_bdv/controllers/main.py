# -*- coding: utf-8 -*-
import json
import logging
import secrets
import time
from odoo import http, fields
from odoo.http import request, Response
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BestpayBDVController(http.Controller):

    # =====================================================
    # 2. WEB: FORMULARIO DE PAGO (Para el usuario final)
    # =====================================================
    @http.route('/pago/bdv/checkout', type='http', auth='none', website=False)
    def bdv_checkout(self, hash=None, **kw):
        if not hash:
            return request.not_found()
        
        transaction = request.env['payment.transaction'].sudo().search([
            ('uuid_hash', '=', hash)
        ], limit=1)
        
        if not transaction.exists():
            return request.not_found()
            
        if transaction.state == 'done':
            return request.render('bestpay_bdv.bdv_checkout_result', {
                'transaction': transaction,
                'status': 'success',
                'message': 'Este pago ya fue procesado exitosamente.'
            })
        
        # --- NUEVA LÓGICA DE ENRUTAMIENTO ---
        method_code = transaction.payment_method_id.code

        if method_code == 'bdv_c2p':
            # Renderiza el NUEVO formulario multi-paso para C2P
            return request.render('bestpay_bdv.c2p_checkout_form', {
                'transaction': transaction,
                'hash': hash,
            })
        else:
            # Renderiza el formulario VIEJO de Pago Móvil (intacto)
            return request.render('bestpay_bdv.bdv_checkout_form', {
                'transaction': transaction,
                'hash': hash,
            })

    # =====================================================
    # 3. WEB: PROCESAR EL PAGO (Envío al BDV)
    # =====================================================
    @http.route('/pago/bdv/procesar', type='http', auth='none', methods=['POST'], csrf=False)
    def bdv_process_payment(self, **post):
        tx_hash = post.get('hash')
        if not tx_hash:
            return request.not_found()
            
        transaction = request.env['payment.transaction'].sudo().search([
            ('uuid_hash', '=', tx_hash)
        ], limit=1)
        
        if not transaction.exists():
            return request.not_found()

        # Mapear datos del formulario a los campos del modelo
        cedula_raw = post.get('cedula', '')
        tipo_cedula = post.get('tipo_cedula', 'V')
        cedula = f"{tipo_cedula}{cedula_raw}"

        telefono_raw = post.get('telefono', '')
        if telefono_raw and not telefono_raw.startswith('0'):
            telefono_pagador = f"0{telefono_raw}"
        else:
            telefono_pagador = telefono_raw

        # =====================================================
        # LÓGICA DE MONTO: QA (Hardcodeado) vs PRODUCCIÓN (Real)
        # =====================================================
        # Leemos el entorno directamente del provider de la transacción
        env_type = transaction.provider_id.bdv_environment.strip().lower() if transaction.provider_id.bdv_environment else 'qa'
        
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

        # Actualizar la transacción con los datos del pagador y el monto determinado
        transaction.write({
            'bdv_cedula_pagador': cedula,
            'bdv_telefono_pagador': telefono_pagador,
            'bdv_banco_origen': post.get('banco', '0102'),
            'bdv_referencia': post.get('referencia', ''),
            'bdv_fecha_pago': fields.Date.today(),
            'bdv_importe': importe_float,  # ← Se usa el monto calculado con seguridad
        })

        # Llamar al método de conciliación (el que creamos en el modelo)
        try:
            is_approved = transaction.bdv_send_conciliation()
            
            if is_approved:
                return request.render('bestpay_bdv.bdv_checkout_result', {
                    'transaction': transaction,
                    'status': 'success',
                    'message': '¡Pago aprobado exitosamente por el Banco de Venezuela!'
                })
            else:
                return request.render('bestpay_bdv.bdv_checkout_result', {
                    'transaction': transaction,
                    'status': 'error',
                    'message': f"Pago rechazado: {transaction.bdv_conciliation_message or 'Revise los datos e intente nuevamente.'}"
                })

        except UserError as e:
            _logger.error(f"[BDV] Error de usuario: {str(e)}")
            return request.render('bestpay_bdv.bdv_checkout_result', {
                'transaction': transaction,
                'status': 'error',
                'message': str(e)
            })
        except Exception as e:
            _logger.error(f"[BDV] Error inesperado: {str(e)}")
            return request.render('bestpay_bdv.bdv_checkout_result', {
                'transaction': transaction,
                'status': 'error',
                'message': 'Ocurrió un error inesperado al procesar el pago.'
            })
    

    # =====================================================
    # 4. WEBHOOK: NOTIFICACIÓN DEL BANCO (Server-to-Server)
    # =====================================================
    @http.route('/api/bestpay/v1/webhook/bdv', type='http', auth='none', methods=['POST', 'OPTIONS'], csrf=False)
    def bdv_webhook_notify(self, **post):
        """
        Endpoint para recibir notificaciones automáticas del BDV.
        Incluye headers CORS para permitir peticiones desde herramientas web (Hoppscotch, BDV QA).
        """
        # Headers CORS obligatorios para que los navegadores no bloqueen la respuesta
        cors_headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'API-Key, Content-Type, Accept',
        }

        # Si el navegador envía una petición OPTIONS (preflight), respondemos 200 inmediatamente
        if request.httprequest.method == 'OPTIONS':
            return Response('', status=200, headers=cors_headers)

        try:
            raw_data = request.httprequest.data.decode('utf-8')
            _logger.info(f"[BDV NOTIFICACIÓN] Webhook recibido. Body: {raw_data}")
            
            # 1. Validar API-KEY del header
            api_key_header = request.httprequest.headers.get('API-KEY', '').strip()
            if not api_key_header:
                return Response(
                    json.dumps({"codigo": "99", "mensajeCliente": "Corrija el API KEY", "mensajeSistema": "Error en API KEY"}),
                    status=200, content_type='application/json', headers=cors_headers
                )

            # 2. Parsear JSON
            try:
                payload = json.loads(raw_data)
            except json.JSONDecodeError:
                return Response(
                    json.dumps({"codigo": "99", "mensajeCliente": "JSON inválido", "mensajeSistema": "Error al parsear"}),
                    status=200, content_type='application/json', headers=cors_headers
                )

            # 3. Identificar el comercio receptor
            numero_comercio = payload.get('numeroComercio', '')
            
            # 4. Validar API Key contra el Partner (Comercio)
            partner = request.env['res.partner'].sudo().search([
                '|',
                ('bdv_telefono_destino', '=', numero_comercio),
                ('bdv_telefono_notificacion', '=', numero_comercio)
            ], limit=1)
            
            api_key_valida = False
            if partner and partner.bdv_api_key_notification:
                api_key_valida = (api_key_header == partner.bdv_api_key_notification)
            else:
                # Respaldo para QA
                api_key_valida = (api_key_header == '97F6F54EF1A84F3A24FE19A3B338C77A')

            if not api_key_valida:
                return Response(
                    json.dumps({"codigo": "99", "mensajeCliente": "Corrija el API KEY", "mensajeSistema": "Error en API KEY"}),
                    status=200, content_type='application/json', headers=cors_headers
                )

            # 5. Delegar la lógica de negocio al modelo (Búsqueda por Teléfono)
            resultado = request.env['payment.transaction'].sudo().bdv_process_notification_webhook(payload, partner)

            # 6. Responder al BDV
            if resultado['codigo'] == '01':
                return Response(
                    json.dumps({"codigo": "01", "mensajeCliente": "pago previamente recibido", "mensajeSistema": "renotificado"}),
                    status=200, content_type='application/json', headers=cors_headers
                )
            else:
                return Response(
                    json.dumps({"codigo": "00", "mensajeCliente": "Aprobado", "mensajeSistema": "Notificado"}),
                    status=200, content_type='application/json', headers=cors_headers
                )

        except Exception as e:
            _logger.error(f"[BDV NOTIFICACIÓN] Error inesperado: {e}", exc_info=True)
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

        try:
            # 1. Guardamos los datos iniciales en la transacción
            transaction.write({
                'bdv_c2p_customer_document_id': customer_document_id,
                'bdv_c2p_customer_phone': customer_phone,
                'bdv_c2p_customer_bank_code': customer_bank_code,
                'bdv_c2p_status': 'processing'
            })

            # 2. Llamamos al método del modelo que ya creamos
            result = transaction.bdv_c2p_generate_otp()
            
            return {
                'success': True,
                'message': result.get('message', 'OTP enviado correctamente. Revise su teléfono.'),
                'status': transaction.bdv_c2p_status
            }

        except UserError as e:
            return {'success': False, 'error': str(e)}
        except Exception as e:
            _logger.error(f"[BDV C2P OTP] Error inesperado: {str(e)}", exc_info=True)
            return {'success': False, 'error': 'Error interno del servidor al generar OTP.'}

    # =====================================================
    # 6. API C2P: PROCESAR PAGO (Paso 2 del flujo multi-paso)
    # =====================================================
    @http.route('/api/bestpay/v1/c2p/process_payment', type='jsonrpc', auth='none', methods=['POST'], csrf=False)
    def c2p_process_payment(self, uuid_hash=None, otp=None, **kw):
        """
        Recibe el OTP del cliente y ejecuta el cobro real en el BDV.
        """
        if not uuid_hash or not otp:
            return {'success': False, 'error': 'Faltan datos obligatorios (uuid_hash u OTP).'}

        transaction = request.env['payment.transaction'].sudo().search([('uuid_hash', '=', uuid_hash)], limit=1)
        if not transaction.exists():
            return {'success': False, 'error': 'Transacción no válida.'}

        try:
            # 1. Guardamos el OTP en la transacción
            transaction.write({
                'bdv_c2p_otp': otp,
                'bdv_c2p_status': 'processing'
            })

            # 2. Llamamos al método del modelo que ejecuta el cobro
            result = transaction.bdv_c2p_process_payment()
            
            if result.get('success'):
                # Generamos la URL de éxito para que el frontend redirija o renderice
                base_url = request.env['ir.config_parameter'].sudo().get_param('web.base.url')
                success_url = f"{base_url}/pago/bdv/checkout?hash={uuid_hash}&status=success"
                
                return {
                    'success': True,
                    'message': 'Pago aprobado exitosamente.',
                    'redirect_url': success_url,
                    'end_to_end_id': transaction.bdv_c2p_end_to_end_id
                }
            else:
                # Si falla, intentamos anular automáticamente para liberar al cliente (buena práctica)
                transaction.bdv_c2p_annul()
                return {
                    'success': False,
                    'error': result.get('message', 'El banco rechazó el pago.')
                }

        except UserError as e:
            return {'success': False, 'error': str(e)}
        except Exception as e:
            _logger.error(f"[BDV C2P Process] Error inesperado: {str(e)}", exc_info=True)
            return {'success': False, 'error': 'Error interno del servidor al procesar el pago.'}
    
        # =====================================================
    # 7. API CONSULTA DE MOVIMIENTOS BDV (Proxy para E-commerce)
    # =====================================================
    @http.route('/api/bestpay/v1/bdv/consulta/movimientos', type='jsonrpc', auth='none', methods=['POST'], csrf=False)
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
            return {'status': 'error', 'message': f'Error interno: {str(e)}'}