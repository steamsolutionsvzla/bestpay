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