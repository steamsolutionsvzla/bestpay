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
    bdv_api_url = fields.Char(
        string="URL API BDV",
        help="Endpoint de la API de conciliación del BDV.",
        default="https://bdvconciliacionqa.banvenez.com:444/getMovement/v2",
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

    bdv_test_date = fields.Char(
        string="Fecha de Prueba QA",
        help="Fecha fija para pruebas en ambiente QA (formato: YYYY-MM-DD). Dejar vacío para usar fecha actual.",
        default="2023-02-12",
    )

    # =====================================================
    # MÉTODOS AUXILIARES
    # =====================================================
    def bdv_get_api_credentials(self, partner=None):
        """
        Devuelve las credenciales configuradas para el BDV.
        Ahora la API Key y el teléfono destino vienen del partner (comercio),
        no del provider.
        
        :param partner: res.partner record (el comercio/cliente)
        :return: dict con las credenciales
        """
        self.ensure_one()
        
        # Obtener API Key y teléfono del partner si se proporciona
        api_key = ''
        telefono_destino = ''
        
        if partner:
            api_key = getattr(partner, 'bdv_api_key', '') or ''
            telefono_destino = getattr(partner, 'bdv_telefono_destino', '') or ''
        
        return {
            'api_key': api_key,
            'api_url': self.bdv_api_url or '',
            'telefono_destino': telefono_destino,
            'environment': self.bdv_environment or 'qa',
            'test_date': self.bdv_test_date or '',
        }