# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

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