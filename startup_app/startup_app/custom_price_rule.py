import copy
import json
from erpnext.accounts.doctype.pricing_rule.pricing_rule import  get_pricing_rule_for_item,  set_transaction_type
from erpnext.stock.get_item_details import get_basic_details, get_default_bom, get_gross_profit, get_item_tax_map, get_item_tax_template, get_party_item_code, get_pos_profile_item_details, get_price_list_rate, process_args, process_string_args, remove_standard_fields, set_valuation_rate, update_bin_details, update_party_blanket_order, update_stock, validate_item_details
import frappe

from frappe.utils import cint, flt,add_days



@frappe.whitelist()
def custom_get_item_details(args, doc=None, for_validate=False, overwrite_warehouse=True):
    print("my custom method is called =================================== ")

    """
    args = {
            "item_code": "",
            "warehouse": None,
            "customer": "",
            "conversion_rate": 1.0,
            "selling_price_list": None,
            "price_list_currency": None,
            "plc_conversion_rate": 1.0,
            "doctype": "",
            "name": "",
            "supplier": None,
            "transaction_date": None,
            "conversion_rate": 1.0,
            "buying_price_list": None,
            "is_subcontracted": 0/1,
            "ignore_pricing_rule": 0/1
            "project": ""
            "set_warehouse": ""
    }
    """

    args = process_args(args)
    for_validate = process_string_args(for_validate)
    overwrite_warehouse = process_string_args(overwrite_warehouse)
    item = frappe.get_cached_doc("Item", args.item_code)
    validate_item_details(args, item)

    if isinstance(doc, str):
        doc = json.loads(doc)

    if doc:
        args["transaction_date"] = doc.get("transaction_date") or doc.get("posting_date")

        if doc.get("doctype") == "Purchase Invoice":
            args["bill_date"] = doc.get("bill_date")

    out = get_basic_details(args, item, overwrite_warehouse)

    get_item_tax_template(args, item, out)
    out["item_tax_rate"] = get_item_tax_map(
        args.company,
        args.get("item_tax_template")
        if out.get("item_tax_template") is None
        else out.get("item_tax_template"),
        as_json=True,
    )

    get_party_item_code(args, item, out)

    if args.get("doctype") in ["Sales Order", "Quotation"]:
        set_valuation_rate(out, args)

    update_party_blanket_order(args, out)

    # Never try to find a customer price if customer is set in these Doctype
    current_customer = args.customer
    if args.get("doctype") in ["Purchase Order", "Purchase Receipt", "Purchase Invoice"]:
        args.customer = None

    out.update(get_price_list_rate(args, item))

    args.customer = current_customer

    if args.customer and cint(args.is_pos):
        out.update(get_pos_profile_item_details(args.company, args, update_data=True))

    if item.is_stock_item:
        update_bin_details(args, out, doc)

    # update args with out, if key or value not exists
    for key, value in out.items():
        if args.get(key) is None:
            args[key] = value

    data = get_pricing_rule_for_item(args, doc=doc, for_validate=for_validate)

    out.update(data)

    if (
        frappe.db.get_single_value("Stock Settings", "auto_create_serial_and_batch_bundle_for_outward")
        and not args.get("serial_and_batch_bundle")
        and (args.get("use_serial_batch_fields") or args.get("doctype") == "POS Invoice")
    ):
        update_stock(args, out, doc)

    if args.transaction_date and item.lead_time_days:
        out.schedule_date = out.lead_time_date = add_days(args.transaction_date, item.lead_time_days)

    if args.get("is_subcontracted"):
        out.bom = args.get("bom") or get_default_bom(args.item_code)

    get_gross_profit(out)
    if args.doctype == "Material Request":
        out.rate = args.rate or out.price_list_rate
        out.amount = flt(args.qty) * flt(out.rate)

    out = remove_standard_fields(out)
    return out





def custom_apply_price_discount_rule(pricing_rule, item_details, args):

    item_details.pricing_rule_for = pricing_rule.rate_or_discount

    if (pricing_rule.margin_type in ["Amount", "Percentage"] and pricing_rule.currency == args.currency) or (
        pricing_rule.margin_type == "Percentage"
    ):
        item_details.margin_type = pricing_rule.margin_type
        item_details.has_margin = True

        if pricing_rule.apply_multiple_pricing_rules and item_details.margin_rate_or_amount is not None:
            item_details.margin_rate_or_amount += pricing_rule.margin_rate_or_amount
        else:
            item_details.margin_rate_or_amount = pricing_rule.margin_rate_or_amount

    if pricing_rule.rate_or_discount == "Rate":
        pricing_rule_rate = 0.0
        if pricing_rule.currency == args.currency:
            pricing_rule_rate = pricing_rule.rate

        if pricing_rule_rate:
            is_blank_uom = pricing_rule.get("uom") != args.get("uom")
            item_details.update({
                "price_list_rate": pricing_rule_rate
                * (args.get("conversion_factor", 1) if is_blank_uom else 1),
            })
        item_details.update({"discount_percentage": 0.0})

    for apply_on in ["Discount Amount", "Discount Percentage"]:
        
        if pricing_rule.rate_or_discount != apply_on:
            continue

        field = frappe.scrub(apply_on)
       
        if pricing_rule.apply_discount_on_rate and item_details.get("discount_percentage"):
            
            item_details[field] += (100 - item_details[field]) * (pricing_rule.get(field, 0) / 100)

        elif args.price_list_rate:
            value = pricing_rule.get(field, 0)
            calculate_discount_percentage = False
           
            if field == "discount_percentage":
                field = "discount_amount"

                # Step 1: Initial discount from price_list_rate
                value = args.price_list_rate * (value / 100)

               
                discount_sum = args.price_list_rate - value
                temp_value = value

                # Step 2: Apply custom discounts from fields in fixed order
                discount_components = [
                    ("custom_trade_mark", getattr(pricing_rule, "custom_trade_mark", 0)),
                    ("custom_p_scheme", getattr(pricing_rule, "custom_p_scheme", 0)),
                    ("custom_freight", getattr(pricing_rule, "custom_freight", 0)),
                    ("custom_extra_discount", getattr(pricing_rule, "custom_extra_discount", 0)),
                ]
                
                
                # First, apply the trade mark discount to the discounted amount
                trade_mark_discount = discount_sum * (float(discount_components[0][1]) / 100)
                
                discount_sum -= trade_mark_discount

                # Then apply custom_a discount to the remaining amount after trade mark
                custom_a_discount = discount_sum * (float(discount_components[1][1]) / 100)
               
                discount_sum -= custom_a_discount

                # Apply custom_b discount to the remaining amount after custom_a
                custom_b_discount = discount_sum * (float(discount_components[2][1]) / 100)
              
                discount_sum -= custom_b_discount

                # Finally, apply custom_c discount to the remaining amount
                custom_c_discount = discount_sum * (float(discount_components[3][1]) / 100)
               
                discount_sum -= custom_c_discount

                value = temp_value + trade_mark_discount + custom_a_discount + custom_b_discount + custom_c_discount 

                gst_price = args.price_list_rate - value 
                
                final_gst_price = gst_price - (gst_price / (1 + (pricing_rule.custom_gst_rate / 100)))
                
                value = value + final_gst_price
                calculate_discount_percentage = True

           
                  
            if field not in item_details:
               
                item_details.setdefault(field, 0)
              
            item_details[field] += value if pricing_rule else args.get(field, 0)
           
            if calculate_discount_percentage and args.price_list_rate and item_details.discount_amount:
                item_details.discount_percentage = flt(
                    (flt(item_details.discount_amount) / flt(args.price_list_rate)) * 100
                )
                
        else:
            
            if field not in item_details:
                item_details.setdefault(field, 0)

            item_details[field] += pricing_rule.get(field, 0) if pricing_rule else args.get(field, 0)





# core method overide 
import erpnext
setattr(erpnext.accounts.doctype.pricing_rule.pricing_rule,"apply_price_discount_rule",custom_apply_price_discount_rule) 



