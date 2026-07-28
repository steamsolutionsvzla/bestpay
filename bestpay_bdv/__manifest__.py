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
        'views/res_partner_views.xml',
        'views/payment_provider_views.xml',
        'views/templates.xml',
        'views/c2p_checkout_form.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'bestpay_bdv/static/src/js/bdv_checkout.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}