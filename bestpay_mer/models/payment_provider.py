# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class PaymentProviderMercantil(models.Model):
    _inherit = 'payment.provider'

    # =====================================================
    # IDENTIFICADOR ÚNICO DEL PROVEEDOR
    # =====================================================
    # Este código es CRÍTICO: Odoo lo usa para saber qué módulo
    # maneja este provider. Debe coincidir con el nombre del módulo.
    code = fields.Selection(
        selection_add=[('mer', 'Banco Mercantil')],
        ondelete={'mer': 'set default'}
    )