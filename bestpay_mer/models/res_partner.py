# -*- coding: utf-8 -*-
from odoo import models, fields

class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Credenciales exclusivas Mercantil por cada cliente de la API
    mercantil_payment_url = fields.Char(
        string="URL Botón de Pago Mercantil",
        help="URL del endpoint del banco (Sandbox o Producción) asignada a este cliente."
    )
    mercantil_integrator_id = fields.Char(
        string="ID Integrador Mercantil",
        help="Identificador del integrador proporcionado por el banco."
    )
    mercantil_merchant_id = fields.Char(
        string="ID Comercio Mercantil (Merchant ID)",
        help="Identificador único del comercio en Mercantil."
    )
    mercantil_secret_key = fields.Char(
        string="Clave Secreta Mercantil (Secret Key)",
        help="Clave secreta utilizada para la encriptación AES-ECB de las transacciones."
    )

    # Configuración de flujos y URLs del tercero
    mercantil_payment_concepts = fields.Char(
        string="Conceptos de Pago por Defecto",
        default='["b2b","c2p","tdd"]',
        help="Formatos de pago permitidos en formato JSON string. Ej: ['b2b','c2p','tdd']"
    )
    default_return_url_3ro = fields.Char(
        string="URL de Retorno por Defecto (3ro)",
        help="URL del ecommerce del tercero a donde el banco redirigirá al cliente final si no se envía una en la API."
    )
    webhook_url_3ro = fields.Char(
        string="URL Webhook del Tercero",
        help="URL del sistema del tercero donde Odoo notificará de forma asíncrona cuando el banco confirme el pago."
    )