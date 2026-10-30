{
    'name': 'BestPay',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Payment',
    'summary': 'Módulo de integración de pagos BestPay',
    'description': 'Verificador de pagos y enlaces de pago para Steam Solutions',
    'author': 'Steam Solution',

    'depends': [
        'base',
        'payment',
    ],
    'data': [
        'data/cron_bcv.xml',
        'data/cron_expire_transactions.xml',
        'data/bestpay_menus.xml',
        'security/ir.model.access.csv',
        'views/res_partner_views.xml',
        'views/payment_transaction_views.xml',
        'views/payment_provider_views.xml',
        'views/res_currency_views.xml', 
    ],

    'assets': {
        'web.assets_backend': [
            'bestpay/static/src/css/password_toggle_field.css',
            'bestpay/static/src/js/password_toggle_field.js',
            'bestpay/static/src/xml/password_toggle_field.xml',
        ],
    },

    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}