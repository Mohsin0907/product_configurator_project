{
    'name': 'Product Configurator',
    'version': '1.0',
    'depends': ['product'],
    'author': 'Mohsin Mohammed',
    'category': 'Inventory',
    'summary': 'On-demand product variant configurator with duplicate check',
    'data': [
        'security/ir.model.access.csv',
        'views/product_configurator_menu.xml',
        'views/product_configurator_wizard_view.xml',
    ],
    'application': True,
}
