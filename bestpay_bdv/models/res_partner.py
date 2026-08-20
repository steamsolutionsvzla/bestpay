# -*- coding: utf-8 -*-
import json
import logging
from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResPartnerBDV(models.Model):
    _inherit = 'res.partner'

    # =====================================================
    # CAMPOS ESPECÍFICOS DEL BANCO DE VENEZUELA (Por Comercio)
    # =====================================================
    # Estos campos antes vivían en el payment.provider, pero como son
    # propios de cada comercio (la API Key la da el banco al comercio
    # y el teléfono destino es donde el comercio recibe los pagos),
    # deben vivir en el partner (cliente).

    bdv_api_key = fields.Char(
        string="API Key BDV (Comercio)",
        help="Clave de autenticación proporcionada por el Banco de Venezuela a este comercio específico.",
        groups="base.group_system",  # Solo admins la ven
        copy=False,
    )

    bdv_telefono_destino = fields.Char(
        string="Teléfono Destino BDV (Comercio)",
        help="Número de teléfono al que este comercio recibe los pagos móviles (formato: 04XXXXXXXXX).",
        copy=False,
    )

    # C2P

    bdv_api_key_c2p = fields.Char(
        string="API Key C2P BDV",
        help="API Key específica para el flujo C2P del Banco de Venezuela"
    )
    
    bdv_phone_destino_c2p = fields.Char(
        string="Teléfono/Cuenta Destino C2P",
        help="Número de teléfono o cuenta destino del comercio para recibir pagos C2P"
    )

        # === API NOTIFICACIÓN ===
    bdv_api_key_notification = fields.Char(
        string="API Key Notificación BDV",
        help="API Key para recibir notificaciones automáticas de pagos del BDV. "
             "En QA usar: 97F6F54EF1A84F3A24FE19A3B338C77A",
        groups="base.group_system",
        copy=False,
    )

    # === API CONSULTA DE MOVIMIENTOS ===
    bdv_api_key_movimientos = fields.Char(
        string="API Key Consulta Movimientos BDV",
        help="API Key para consultar movimientos de cuenta en el BDV. "
             "En QA usar: 256D0FDD36F1B1B3F1208A9B6EC693",
        groups="base.group_system",
        copy=False,
    )

        # =================================================================
    # 🔔 CONFIGURACIÓN DE WEBHOOK AL TERCERO (BestPay → Koole/ecommerce)
    # =================================================================
    # TODO [MIGRACIÓN FASE 7]: Mover estos 3 campos al módulo base 'bestpay'
    # para que sean compartidos por BDV y Mercantil. Al hacerlo, eliminarlos
    # de aquí y ajustar las vistas. Wilson ya tiene nombres similares en
    # 'bestpay_mer/models/res_partner.py' que también deberían migrarse.
    webhook_url_3ro = fields.Char(
        string="URL Webhook del Tercero",
        help="URL del sistema del tercero (ej. Koole) donde BestPay notificará "
             "de forma asíncrona cuando el pago sea confirmado por el banco.",
        copy=False,
    )
    default_return_url_3ro = fields.Char(
        string="URL de Retorno por Defecto (3ro)",
        help="URL a la que el checkout redirigirá al pagador tras completar "
             "el flujo (éxito/error). Se usará en la Fase 5.",
        copy=False,
    )
    bestpay_webhook_secret = fields.Char(
        string="Secreto Webhook (HMAC-SHA256)",
        help="Secreto compartido usado para firmar los webhooks salientes. "
             "El tercero debe conocer este valor para validar la autenticidad.",
        copy=False,
        groups="base.group_system",
    )
    bestpay_webhook_active = fields.Boolean(
        string="Webhook Activo",
        default=True,
        help="Desmarca para suspender los envíos de webhook a este tercero.",
    )

        # =================================================================
    # ⚠️ NOTA: MÉTODO TEMPORAL - Usa Wizard TransientModel
    # =================================================================
    # Este método es temporal para QA. En producción se migrará a:
    # - Modelo persistente (bestpay_bdv.movimiento)
    # - Conciliación automática con payment.transaction
    # - Historial y reportes
    # =================================================================
    
    def action_bdv_consultar_movimientos(self):
        self.ensure_one()
        
        provider = self.env['payment.provider'].sudo().search([
            ('code', '=', 'bdv'),
            ('is_bestpay_provider', '=', True),
        ], limit=1)
        
        if not provider:
            raise UserError("No hay proveedor BDV configurado en el sistema.")
        
        # ==========================================================
        # CORRECCIÓN DEFINITIVA PARA QA: Usar EXACTAMENTE los datos del PDF
        # El ambiente Dummy del BDV solo responde con datos de prueba en este rango.
        # ==========================================================
        cuenta = '01020501830003283374'
        fecha_ini = '01/01/2025'
        fecha_fin = '28/01/2025'
        
        resultado = provider.bdv_consultar_movimientos(
            cuenta=cuenta,
            fecha_ini=fecha_ini,
            fecha_fin=fecha_fin,
            partner=self,
        )
        
        if not resultado.get('success'):
            raise UserError(f"Error consultando movimientos: {resultado.get('message')}")
        
        movements = resultado.get('movements', [])
        
        if not movements:
            raise UserError(f"No hay movimientos de prueba para la cuenta {cuenta} en el rango indicado.")
        
        return {
            'type': 'ir.actions.act_window',
            'name': f'Movimientos BDV - {self.name} (Prueba QA)',
            'res_model': 'bestpay_bdv.movimiento.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_id': self.id,
                'default_movements_json': json.dumps(movements, ensure_ascii=False),
            }
        }