/** @odoo-module **/
import { registry } from "@web/core/registry";
import { CharField } from "@web/views/fields/char/char_field";
import { useState } from "@odoo/owl";

export class PasswordToggleField extends CharField {
    static template = "bestpay_bdv.PasswordToggleField";

    setup() {
        super.setup();
        this.toggleState = useState({ visible: false });
    }

    get inputType() {
        return this.toggleState.visible ? "text" : "password";
    }

    toggleVisibility() {
        this.toggleState.visible = !this.toggleState.visible;
    }

    onInput(ev) {
        this.props.record.update({ [this.props.name]: ev.target.value });
    }
}

registry.category("fields").add("password_toggle", {
    component: PasswordToggleField,
    supportedTypes: ["char"],
});