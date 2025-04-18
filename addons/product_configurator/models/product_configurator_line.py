from odoo import models, fields

class ProductConfiguratorLine(models.TransientModel):
    _name = 'product.configurator.line'
    _description = 'Configurator Line'

    wizard_id = fields.Many2one('product.configurator.wizard', ondelete='cascade')
    attribute_id = fields.Many2one('product.attribute', string="Attribute", readonly=True)
    value_ids = fields.Many2many('product.attribute.value', string="Possible Values", readonly=True)
    value_id = fields.Many2one('product.attribute.value', string="Selected Value")
