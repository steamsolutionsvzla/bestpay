# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class PaymentProviderBDV(models.Model):
    _inherit = 'payment.provider'

    # =====================================================
    # IDENTIFICADOR ÚNICO DEL PROVEEDOR
    # =====================================================
    # Este código es CRÍTICO: Odoo lo usa para saber qué módulo
    # maneja este provider. Debe coincidir con el nombre del módulo.
    code = fields.Selection(
        selection_add=[('bdv', 'Banco de Venezuela')],
        ondelete={'bdv': 'set default'}
    )

    # =====================================================
    # CAMPOS ESPECÍFICOS DEL BANCO DE VENEZUELA
    # =====================================================
    bdv_api_key = fields.Char(
        string="API Key BDV",
        help="Clave de autenticación proporcionada por el Banco de Venezuela.",
        groups="base.group_system",  # Solo admins la ven
    )
    bdv_api_url = fields.Char(
        string="URL API BDV",
        help="Endpoint de la API de conciliación del BDV.",
        default="https://bdvconciliacionqa.banvenez.com:444/getMovement/v2",
    )
    bdv_telefono_destino = fields.Char(
        string="Teléfono Destino (Pago Móvil)",
        help="Número de teléfono al que se recibe el pago móvil (formato: 04XXXXXXXXX).",
        default="04127141363",
    )
    bdv_environment = fields.Selection(
        selection=[
            ('qa', 'QA (Pruebas)'),
            ('prod', 'Producción'),
        ],
        string="Entorno BDV",
        default='qa',
        help="Define si se usa el ambiente de pruebas o producción del BDV.",
    )

    # =====================================================
    # MÉTODOS AUXILIARES
    # =====================================================
    def bdv_get_api_credentials(self):
        """Devuelve las credenciales configuradas para el BDV."""
        self.ensure_one()
        return {
            'api_key': self.bdv_api_key or '',
            'api_url': self.bdv_api_url or '',
            'telefono_destino': self.bdv_telefono_destino or '',
            'environment': self.bdv_environment or 'qa',
        }