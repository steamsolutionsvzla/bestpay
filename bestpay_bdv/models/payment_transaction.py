# -*- coding: utf-8 -*-
import json
import logging
import requests
from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PaymentTransactionBDV(models.Model):
    _inherit = 'payment.transaction'

    payment_link = fields.Char(
        string="Link de Pago",
        help="URL completa del formulario de pago que se envía al cliente final.",
        copy=False,
        readonly=True,
    )

    # =====================================================
    # CAMPOS ESPECÍFICOS DEL FLUJO BDV (Pago Móvil)
    # =====================================================
    # Estos son los datos que el BDV pide para conciliar el pago
    bdv_cedula_pagador = fields.Char(
        string="Cédula Pagador BDV",
        help="Cédula de quien realiza el pago móvil (ej: V12345678).",
    )
    bdv_telefono_pagador = fields.Char(
        string="Teléfono Pagador BDV",
        help="Teléfono de quien realiza el pago móvil (ej: 04121234567).",
    )
    bdv_banco_origen = fields.Char(
        string="Banco Origen BDV",
        help="Código del banco de donde sale el pago (ej: 0102 = BDV, 0105 = Mercantil).",
    )
    bdv_referencia = fields.Char(
        string="Referencia BDV",
        help="Número de referencia del pago móvil proporcionado por el pagador.",
    )
    bdv_fecha_pago = fields.Date(
        string="Fecha del Pago BDV",
        help="Fecha en que se realizó el pago móvil.",
    )
    bdv_importe = fields.Float(
        string="Importe BDV (VES)",
        help="Monto en Bolívares que el pagador dice haber transferido.",
        digits=(12, 2),
    )
    bdv_req_ced = fields.Boolean(
        string="Requiere Cédula BDV",
        default=False,
    )

    # =====================================================
    # ESTADO DEL PROCESO DE CONCILIACIÓN
    # =====================================================
    bdv_conciliation_state = fields.Selection([
        ('draft', 'Borrador'),
        ('sent', 'Enviado al BDV'),
        ('approved', 'Aprobado (1000)'),
        ('rejected', 'Rechazado (1010)'),
        ('error', 'Error de conexión'),
    ], string="Estado Conciliación BDV", default='draft')

    bdv_conciliation_message = fields.Char(
        string="Mensaje del BDV",
        help="Mensaje devuelto por el BDV en la última conciliación.",
    )

    def bdv_send_conciliation(self):
        """
        Envía los datos del pago móvil al BDV para conciliación.
        Devuelve True si fue aprobado, False en caso contrario.
        """
        self.ensure_one()
        provider = self.provider_id

        if provider.code != 'bdv':
            raise UserError("Este método solo aplica para el proveedor BDV.")

        # 1. Obtener credenciales del provider, pasando el partner (comercio)
        # para que tome la API Key y teléfono destino del partner, no del provider
        creds = provider.bdv_get_api_credentials(partner=self.partner_id)
        
        if not creds['api_key']:
            raise UserError("Falta configurar la API Key del BDV en el proveedor de pago.")

        # 2. Construir el payload que pide el BDV
        # Usar fecha de prueba si está configurada, sino usar fecha actual
        test_date = creds.get('test_date')
        fecha_pago = test_date if test_date else str(self.bdv_fecha_pago or fields.Date.today())
        
        payload = {
            "cedulaPagador": self.bdv_cedula_pagador or '',
            "telefonoPagador": self.bdv_telefono_pagador or '',
            "telefonoDestino": creds['telefono_destino'],
            "referencia": self.bdv_referencia or '',
            "fechaPago": fecha_pago,  # ← CAMBIAR ESTA LÍNEA
            "importe": f"{self.bdv_importe:.2f}",
            "bancoOrigen": self.bdv_banco_origen or '',
            "reqCed": self.bdv_req_ced,
        }

        headers = {
            "X-API-Key": creds['api_key'],
            "Content-Type": "application/json",
        }

        _logger.info(f"[BDV] Enviando conciliación para TX {self.id}: {json.dumps(payload)}")

        # 3. Marcar como "enviado"
        self.write({
            'bdv_conciliation_state': 'sent',
            'bank_out_log': json.dumps(payload, indent=2),
        })

        # 4. Hacer la petición HTTP al BDV
        try:
            response = requests.post(
                creds['api_url'],
                json=payload,
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            respuesta = response.json()

            _logger.info(f"[BDV] Respuesta recibida: {respuesta}")

            # 5. Guardar la respuesta cruda
            self.bank_in_log = json.dumps(respuesta, indent=2, ensure_ascii=False)

            # 6. Procesar el código de respuesta del BDV
            code = respuesta.get("code")
            message = respuesta.get("message", "")

            if code == 1000:
                # ✅ PAGO APROBADO
                self.write({
                    'bdv_conciliation_state': 'approved',
                    'bdv_conciliation_message': message,
                    'state': 'done',
                    'bank_in_log': json.dumps(respuesta, indent=2, ensure_ascii=False),
                })
                _logger.info(f"[BDV] ✅ TX {self.id} APROBADA por el BDV")
                return True

            elif code == 1010:
                # ❌ PAGO RECHAZADO
                self.write({
                    'bdv_conciliation_state': 'rejected',
                    'bdv_conciliation_message': message,
                    'state': 'cancel',
                    'bank_in_log': json.dumps(respuesta, indent=2, ensure_ascii=False),
                })
                _logger.warning(f"[BDV] ❌ TX {self.id} RECHAZADA: {message}")
                return False

            else:
                # ⚠️ CÓDIGO DESCONOCIDO
                self.write({
                    'bdv_conciliation_state': 'error',
                    'bdv_conciliation_message': f"Código inesperado: {code} - {message}",
                    'bank_in_log': json.dumps(respuesta, indent=2, ensure_ascii=False),
                })
                _logger.error(f"[BDV] ⚠️ TX {self.id} código inesperado: {code}")
                return False

        except requests.exceptions.RequestException as e:
            # ❌ ERROR DE CONEXIÓN - Sin simulación, solo registro del error
            _logger.error(f"[BDV] Error de conexión: {str(e)}")
            
            self.write({
                'bdv_conciliation_state': 'error',
                'bdv_conciliation_message': f"Error de conexión: {str(e)}",
                'state': 'error',
                'bank_in_log': json.dumps({
                    'error': str(e),
                    'payload_sent': payload,
                }, indent=2, ensure_ascii=False),
            })
            raise UserError(f"Error de conexión con el BDV: {str(e)}")
    
    @api.model_create_multi
    def create(self, vals_list):
        """
        Sobrescribe create() para asignar automáticamente payment_method_id
        cuando el proveedor es BDV. Resuelve la restricción NOT NULL de Odoo 19
        sin modificar el módulo base de BestPay.
        """
        for vals in vals_list:
            provider_id = vals.get('provider_id')
            if provider_id and not vals.get('payment_method_id'):
                provider = self.env['payment.provider'].sudo().browse(provider_id)
                if provider.code == 'bdv':
                    # Buscar o crear el método de pago BDV
                    pm = self.env['payment.method'].sudo().search([
                        ('code', '=', 'bdv_pagomovil'),
                        ('active', '=', True)
                    ], limit=1)
                    
                    if not pm:
                        pm = self.env['payment.method'].sudo().create({
                            'name': 'Pago Móvil BDV',
                            'code': 'bdv_pagomovil',
                            'active': True,
                        })
                        _logger.info(f"[BDV] Método de pago creado: {pm.name}")
                    
                    vals['payment_method_id'] = pm.id
                    _logger.info(f"[BDV] payment_method_id asignado automáticamente: {pm.id}")
        
        return super().create(vals_list) 
    
    def _bestpay_process_transaction_with_bank(self, data):
        """
        Método llamado por el endpoint base de BestPay.
        Usa el uuid_hash existente para construir el link de pago.
        """
        self.ensure_one()

        # Si NO es BDV, le pasamos el control al siguiente módulo en la cadena (Mercantil)
        if self.provider_id.code != 'bdv':
            return super()._bestpay_process_transaction_with_bank(data)

        # Si SÍ es BDV, procesamos nuestra lógica
        _logger.info("[BDV] Procesando transacción %s", self.id)

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        payment_link = f"{base_url}/pago/bdv/checkout?hash={self.uuid_hash}"

        self.write({
            'payment_link': payment_link,
            'bestpay_flow_type': 'redirect',
        })

        _logger.info("[BDV] Link generado con UUID_HASH para TX %s: %s", self.id, payment_link)

        return {
            'payment_link': payment_link,
            'uuid_hash': self.uuid_hash,
            'flow_type': 'redirect',
        }