{
    'name': 'BestPay - Banco de Venezuela',
    'version': '19.0.1.0.1',
    'category': 'Accounting/Payment',
    'summary': 'Integración con Banco de Venezuela para BestPay',
    'author': 'Samuel Guillén',
    'depends': [
        'payment',
        'bestpay',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/payment_provider_data.xml',
        'data/ir_cron.xml',                  
        'views/res_partner_views.xml',
        'views/payment_provider_views.xml',
        'views/templates.xml',
        'views/c2p_checkout_form.xml',
        'views/movimiento_wizard_views.xml',
    ],
        'assets': {
        'web.assets_frontend': [
            'bestpay_bdv/static/src/js/bdv_checkout.js',
        ],
        'web.assets_backend': [
            'bestpay_bdv/static/src/css/password_toggle_field.css',
            'bestpay_bdv/static/src/js/password_toggle_field.js',
            'bestpay_bdv/static/src/xml/password_toggle_field.xml',
        ],
    },

    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}