# -*- coding: utf-8 -*-
from odoo import models, fields, api
import requests
from bs4 import BeautifulSoup
import logging

_logger = logging.getLogger(__name__)

class ResCurrency(models.Model):
    _inherit = 'res.currency'

    @api.model
    def _update_bcv_rate(self):
        _logger.info('[BCV SYNC] ACTUALIZANDO ODOO NATIVO (MONEDA BASE = USD)')
        
        url = "https://www.bcv.org.ve/"
        headers = {'User-Agent': 'Mozilla/5.0'}

        try:
            response = requests.get(url, headers=headers, verify=False, timeout=30)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, 'html.parser')
                today = fields.Date.today()
                
                # Extraemos los contenedores de la web
                section_dolar = soup.find('div', id='dolar')
                section_euro = soup.find('div', id='euro')
                
                if section_dolar:
                    texto_dolar = section_dolar.find('strong').text.strip()
                    tasa_dolar = float(texto_dolar.replace(',', '.'))
                    
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
                                'rate': tasa_dolar, # Aquí sí va directo (639.70)
                                'company_id': self.env.company.id,
                            })
                            _logger.info(f'[BCV SYNC] ✓ VES guardado directo: {tasa_dolar}')

                        # 2. EN BASE USD: El Euro se calcula cruzado (Tasa Dólar / Tasa Euro)
                        if section_euro:
                            texto_euro = section_euro.find('strong').text.strip()
                            tasa_euro = float(texto_euro.replace(',', '.'))
                            
                            if tasa_euro > 0:
                                tasa_final_eur = tasa_dolar / tasa_euro
                                
                                currency_eur = self.env['res.currency'].search([('name', '=', 'EUR')], limit=1)
                                if currency_eur:
                                    self.env['res.currency.rate'].search([
                                        ('currency_id', '=', currency_eur.id), ('name', '=', today), ('company_id', '=', self.env.company.id)
                                    ]).unlink()
                                    
                                    self.env['res.currency.rate'].create({
                                        'currency_id': currency_eur.id,
                                        'name': today,
                                        'rate': tasa_final_eur,
                                        'company_id': self.env.company.id,
                                    })
                                    _logger.info(f'[BCV SYNC] ✓ EUR guardado cruzado: {tasa_final_eur}')
                                    
            self.env.invalidate_all()
        except Exception as e:
            _logger.error(f"[BCV SYNC] Error: {str(e)}")