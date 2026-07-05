{
    'name': 'BestPay - Banco de Venezuela',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Payment',
    'summary': 'Integración con Banco de Venezuela para BestPay',
    'author': 'Samuel Guillén',
    'depends': [
        'payment',
        'bestpay',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/payment_provider_views.xml',
        'views/templates.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}