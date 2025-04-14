from odoo import models, fields, api

class ProductConfiguratorWizard(models.TransientModel):
    _name = 'product.configurator.wizard'
    _description = 'Product Configurator Wizard'

    product_tmpl_id = fields.Many2one(
        'product.template', string="Product Template", required=True,
        domain="[('create_variant_ids', '=', 'dynamic')]"
    )
    attribute_value_ids = fields.Many2many('product.template.attribute.value', string="Attributes")
    default_code = fields.Char(string="Internal Reference")
    barcode = fields.Char(string="Barcode")
    existing_product_id = fields.Many2one('product.product', string="Existing Variant", readonly=True)

    @api.onchange('product_tmpl_id')
    def _onchange_product_tmpl_id(self):
        if self.product_tmpl_id:
            return {'domain': {
                'attribute_value_ids': [('attribute_id', 'in', self.product_tmpl_id.attribute_line_ids.mapped('attribute_id').ids)]
            }}

    @api.onchange('attribute_value_ids')
    def _onchange_attribute_value_ids(self):
        self.existing_product_id = False
        if self.product_tmpl_id and self.attribute_value_ids:
            for variant in self.product_tmpl_id.product_variant_ids:
                if set(variant.product_template_attribute_value_ids.ids) == set(self.attribute_value_ids.ids):
                    self.existing_product_id = variant.id
                    break

    def create_variant(self):
        self.ensure_one()

        if self.existing_product_id:
            return {
                'type': 'ir.actions.act_window',
                'name': 'Existing Variant',
                'res_model': 'product.product',
                'res_id': self.existing_product_id.id,
                'view_mode': 'form',
                'target': 'current'
            }

        attribute_value_ids = self.attribute_value_ids
        combination = self.product_tmpl_id._create_variant_ids(attribute_value_ids)

        combination.default_code = self.default_code
        combination.barcode = self.barcode

        # ➡️ Show success notification
        message = f"Variant {combination.display_name} successfully created."
        self.env.user.notify_info(message)

        return {
            'type': 'ir.actions.act_window',
            'name': 'Product Variant',
            'res_model': 'product.product',
            'res_id': combination.id,
            'view_mode': 'form',
            'target': 'current'
        }
