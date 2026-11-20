# -*- coding: utf-8 -*-
from odoo import models, fields, api
import re
import requests
from bs4 import BeautifulSoup
import logging

from ..utils.business_logger import log_business_error

_logger = logging.getLogger(__name__)


def _parse_bcv_rate(raw):
    """Convierte el texto de una tasa del BCV a ``float``.

    El BCV publica en formato venezolano: ``.`` como separador de miles y
    ``,`` como separador decimal (ej. ``1.009,07172979``). También se tolera
    el formato con punto decimal (ej. ``36.5682``) por si cambian la web.

    :raises ValueError: si el texto no contiene un número interpretable.
    """
    if raw is None:
        raise ValueError('texto de tasa vacío')

    texto = re.sub(r'[^\d.,-]', '', str(raw)).strip()
    if not any(caracter.isdigit() for caracter in texto):
        raise ValueError('el texto de tasa no contiene dígitos: %r' % (raw,))

    if ',' in texto and '.' in texto:
        # El último separador que aparece es el decimal; el otro es de miles.
        decimal = ',' if texto.rfind(',') > texto.rfind('.') else '.'
        miles = '.' if decimal == ',' else ','
        texto = texto.replace(miles, '').replace(decimal, '.')
    elif ',' in texto:
        # Formato venezolano: la coma siempre es decimal.
        texto = texto.replace('.', '').replace(',', '.')
    elif texto.count('.') > 1:
        # Varios puntos y ninguna coma. Si todos los grupos son de 3 dígitos
        # son separadores de miles; si no, el último punto es el decimal.
        grupos = texto.split('.')
        if all(len(grupo) == 3 for grupo in grupos[1:]):
            texto = texto.replace('.', '')
        else:
            texto = ''.join(grupos[:-1]) + '.' + grupos[-1]
    elif texto.count('.') == 1:
        entero, decimales = texto.split('.')
        # '1.009' son miles; '36.5682' es decimal (el BCV usa 4+ decimales).
        if len(decimales) == 3 and entero.lstrip('-').isdigit():
            texto = texto.replace('.', '')

    return float(texto)


class ResCurrency(models.Model):
    _inherit = 'res.currency'

    bestpay_eur_bcv_rate = fields.Float(
        string='Tasa BCV Euro (Directa)',
        help='Tasa cruda del Euro extraída del BCV (sin cruces de Odoo)',
        digits=(16, 4),
    )

    @api.model
    def _update_bcv_rate(self):
        _logger.info('[BCV SYNC] ACTUALIZANDO ODOO NATIVO (MONEDA BASE = USD)')

        url = "https://www.bcv.org.ve/"
        headers = {'User-Agent': 'Mozilla/5.0'}

        try:
            response = requests.get(url, headers=headers, verify=False, timeout=30)
            if response.status_code != 200:
                log_business_error(
                    self.env,
                    '_update_bcv_rate',
                    input_data={'url': url, 'http_status': response.status_code},
                    output_data=(
                        'El BCV respondió con HTTP %s; la tasa NO se actualizó.'
                        % response.status_code
                    ),
                    is_error=True,
                    error_type='BCVHttpError',
                    error_trace='Respuesta HTTP %s de %s' % (
                        response.status_code, url),
                    dedupe_key='bestpay_bcv_http',
                    dedupe_minutes=60,
                    meta={'servicio': 'BCV', 'url': url},
                )
                return

            soup = BeautifulSoup(response.content, 'html.parser')
            today = fields.Date.today()

            # Extraemos los contenedores de la web
            section_dolar = soup.find('div', id='dolar')
            section_euro = soup.find('div', id='euro')

            if not section_dolar:
                # Sin tasa nueva, todas las facturas del día salen con el
                # valor de ayer. Sí amerita aviso (con dedupe).
                log_business_error(
                    self.env,
                    '_update_bcv_rate',
                    input_data={'url': url, 'http_status': response.status_code},
                    output_data=(
                        'No se encontró el contenedor #dolar en la web del BCV; '
                        'la tasa NO se actualizó.'
                    ),
                    is_error=True,
                    error_type='BCVLayoutChanged',
                    error_trace='No aparece el div#dolar en https://www.bcv.org.ve/',
                    dedupe_key='bestpay_bcv_layout',
                    dedupe_minutes=180,
                    meta={'servicio': 'BCV', 'url': url},
                )
                return

            texto_dolar = None
            try:
                texto_dolar = section_dolar.find('strong').text.strip()
                tasa_dolar = _parse_bcv_rate(texto_dolar)
                if tasa_dolar <= 0:
                    raise ValueError('la tasa USD no es positiva: %s' % tasa_dolar)
            except Exception as e:
                log_business_error(
                    self.env,
                    '_update_bcv_rate',
                    input_data={'url': url, 'texto': texto_dolar},
                    output_data=(
                        'No se pudo interpretar la tasa USD del BCV: %s; '
                        'la tasa NO se actualizó.' % e
                    ),
                    exc=e,
                    is_error=True,
                    error_type='BCVRateParseError',
                    dedupe_key='bestpay_bcv_parse_usd',
                    dedupe_minutes=60,
                    meta={'servicio': 'BCV', 'url': url, 'moneda': 'USD'},
                )
                return

            if tasa_dolar > 0:
                # 1. EN BASE USD: La tasa del Bolívar (VES) es el valor DIRECTO del BCV
                currency_ves = self.env['res.currency'].search([('name', '=', 'VES')], limit=1)
                if currency_ves:
                    self.env['res.currency.rate'].search([
                        ('currency_id', '=', currency_ves.id), ('name', '=', today), ('company_id', '=', self.env.company.id)
                    ]).unlink()

                    self.env['res.currency.rate'].create({
                        'currency_id': currency_ves.id,
                        'name': today,
                        'rate': tasa_dolar,  # Aquí sí va directo (639.70)
                        'company_id': self.env.company.id,
                    })
                    _logger.info(f'[BCV SYNC] ✓ VES guardado directo: {tasa_dolar}')
                else:
                    log_business_error(
                        self.env,
                        '_update_bcv_rate',
                        input_data={'url': url},
                        output_data=(
                            'No existe la moneda VES en la base de datos; '
                            'la tasa no se pudo guardar.'
                        ),
                        is_error=True,
                        error_type='BCVCurrencyMissing',
                        dedupe_key='bestpay_bcv_currency_missing',
                        dedupe_minutes=1440,
                        meta={'servicio': 'BCV'},
                    )

                # 2. EN BASE USD: El Euro se calcula cruzado (Tasa Dólar / Tasa Euro)
                if section_euro:
                    texto_euro = None
                    try:
                        texto_euro = section_euro.find('strong').text.strip()
                        tasa_euro = _parse_bcv_rate(texto_euro)
                    except Exception as e:
                        tasa_euro = 0.0
                        log_business_error(
                            self.env,
                            '_update_bcv_rate',
                            input_data={'url': url, 'texto': texto_euro},
                            output_data=(
                                'No se pudo interpretar la tasa EUR del BCV: %s; '
                                'la tasa del Euro NO se actualizó (el Bolívar sí).'
                                % e
                            ),
                            exc=e,
                            is_error=True,
                            error_type='BCVRateParseError',
                            dedupe_key='bestpay_bcv_parse_eur',
                            dedupe_minutes=60,
                            meta={'servicio': 'BCV', 'url': url, 'moneda': 'EUR'},
                        )

                    if tasa_euro > 0:
                        tasa_final_eur = tasa_dolar / tasa_euro

                        currency_eur = self.env['res.currency'].search([('name', '=', 'EUR')], limit=1)
                        if currency_eur:
                            currency_eur.write({'bestpay_eur_bcv_rate': tasa_euro})
                            self.env['res.currency.rate'].search([
                                ('currency_id', '=', currency_eur.id), ('name', '=', today), ('company_id', '=', self.env.company.id)
                            ]).unlink()

                            self.env['res.currency.rate'].create({
                                'currency_id': currency_eur.id,
                                'name': today,
                                'rate': tasa_final_eur,
                                'company_id': self.env.company.id,
                                'bestpay_bcv_rate': tasa_euro,
                            })
                            _logger.info(f'[BCV SYNC] ✓ EUR guardado cruzado: {tasa_final_eur}')

            self.env.invalidate_all()
        except Exception as e:
            _logger.error(f"[BCV SYNC] Error: {str(e)}")
            log_business_error(
                self.env,
                '_update_bcv_rate',
                input_data={'url': url},
                output_data='Excepción sincronizando la tasa BCV: %s' % e,
                exc=e,
                is_error=True,
                dedupe_key='bestpay_bcv_exception',
                dedupe_minutes=60,
                meta={'servicio': 'BCV', 'url': url},
            )

    class ResCurrencyRate(models.Model):
        _inherit = 'res.currency.rate'

        bestpay_bcv_rate = fields.Float(
            string='Tasa BCV',
            help='Tasa directa del BCV para el histórico',
            digits=(16, 4),
        )
