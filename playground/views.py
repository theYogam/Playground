# -*- coding: utf-8 -*-
import logging
from datetime import datetime
from threading import Thread

import requests
from currencies.context_processors import currencies
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.mail import EmailMessage
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.http.response import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _, activate

from ikwen.accesscontrol.backends import UMBRELLA
from ikwen.accesscontrol.models import SUDO, Member
from ikwen.core.models import Service, Application
from ikwen.core.utils import get_service_instance, add_event, add_database, set_counters, increment_history_field, \
    get_mail_content, XEmailMessage
from ikwen.rewarding.models import Reward
from ikwen.rewarding.utils import reward_member

from ikwen_kakocase.kako.utils import mark_duplicates
from ikwen_kakocase.kakocase.models import OperatorProfile, SOLD_OUT_EVENT, NEW_ORDER_EVENT
from ikwen_kakocase.kako.models import Product
from ikwen_kakocase.shopping.utils import parse_order_info, send_order_confirmation_sms
from ikwen_kakocase.shopping.models import Customer
from ikwen_kakocase.shopping.views import Cart
from ikwen_kakocase.trade.models import Order
from ikwen_kakocase.trade.utils import generate_tx_code

from daraja.models import Dara


from daraja.models import DARAJA, REFEREE_JOINED_EVENT

logger = logging.getLogger('ikwen')


def set_momo_order_checkout(request, payment_mean, *args, **kwargs):
    """
    This function has no URL associated with it.
    It serves as ikwen setting "MOMO_BEFORE_CHECKOUT"
    """
    service = get_service_instance()
    config = service.config
    if getattr(settings, 'DEBUG', False):
        order = parse_order_info(request)
    else:
        try:
            order = parse_order_info(request)
        except:
            return HttpResponseRedirect(reverse('shopping:checkout'))
    order.retailer = service
    order.payment_mean = payment_mean
    order.save()  # Save first to generate the Order id
    order = Order.objects.get(pk=order.id)  # Grab the newly created object to avoid create another one in subsequent save()

    member = request.user
    if member.is_authenticated():
        order.member = member
    else:
        order.aotc = generate_tx_code(order.id, order.anonymous_buyer.auto_inc)

    order.rcc = generate_tx_code(order.id, config.rel_id)
    order.save()
    request.session['object_id'] = order.id
    request.session['amount'] = order.total_cost


def confirm_checkout(request, *args, **kwargs):
    order_id = request.session.get('object_id')
    order = get_object_or_404(Order, pk=order_id)

    order.status = Order.PENDING
    order.save()

    after_order_confirmation(order)
    member = order.member
    buyer_name = member.full_name
    buyer_email = order.delivery_address.email
    buyer_phone = order.delivery_address.phone

    activate(member.language)
    subject = _("Order successful")
    reward_pack_list, coupon_count = reward_member(order.retailer, member, Reward.PAYMENT,
                                                   amount=order.items_cost, model_name='trade.Order')
    try:
        dara = Dara.objects.using(UMBRELLA).get(member=member)
    except:
        dara = None
    send_order_confirmation_email(request, subject, buyer_name, buyer_email, dara, order, reward_pack_list=reward_pack_list)
    if getattr(settings, 'UNIT_TESTING', False):
        send_order_confirmation_sms(buyer_name, buyer_phone, order)
    else:
        Thread(target=send_order_confirmation_sms, args=(buyer_name, buyer_phone, order)).start()

    return HttpResponse("Notification received")


def after_order_confirmation(order, update_stock=True):
    member = order.member
    service = get_service_instance()
    config = service.config
    delcom = order.delivery_option.company
    delcom_db = delcom.database
    add_database(delcom_db)
    delcom_profile_original = OperatorProfile.objects.using(delcom_db).get(pk=delcom.config.id)
    dara, dara_service_original, provider_mirror = None, None, None
    sudo_group = Group.objects.get(name=SUDO)
    customer = member.customer
    referrer = customer.referrer
    referrer_share_rate = 0

    # Test if the customer has been referred
    if referrer:
        referrer_db = referrer.database
        add_database(referrer_db)
        try:
            dara = Dara.objects.get(member=referrer.member)
        except Dara.DoesNotExist:
            logging.error("%s - Dara %s not found" % (service.project_name, member.username))
        try:
            dara_service_original = Service.objects.using(referrer_db).get(pk=referrer.id)
        except Dara.DoesNotExist:
            logging.error("%s - Dara service not found in %s database for %s" % (service.project_name, referrer_db, referrer.project_name))
        try:
            provider_mirror = Service.objects.using(referrer_db).get(pk=service.id)
        except Service.DoesNotExist:
            logging.error("%s - Provider Service not found in %s database for %s" % (service.project_name, referrer_db, referrer.project_name))

    packages_info = order.split_into_packages(dara)

    for provider_db in packages_info.keys():
        package = packages_info[provider_db]['package']
        provider_earnings = package.provider_earnings
        raw_provider_revenue = package.provider_revenue
        provider_revenue = raw_provider_revenue
        if package.provider == delcom:
            provider_earnings += order.delivery_earnings
            provider_revenue += order.delivery_earnings
        provider_profile_umbrella = packages_info[provider_db]['provider_profile']
        provider_profile_original = provider_profile_umbrella.get_from(provider_db)
        provider_original = provider_profile_original.service

        if delcom == service:
            provider_original.raise_balance(provider_earnings, provider=order.payment_mean.slug)
        else:
            if delcom_profile_original.return_url:
                nvp_dict = package.get_nvp_api_dict()
                Thread(target=lambda url, data: requests.post(url, data=data),
                       args=(delcom_profile_original.return_url, nvp_dict)).start()
            if provider_profile_original.payment_delay == OperatorProfile.STRAIGHT:
                if package.provider_earnings > 0:
                    provider_original.raise_balance(provider_earnings, provider=order.payment_mean.slug)
        if provider_profile_original.return_url:
            nvp_dict = package.get_nvp_api_dict()
            Thread(target=lambda url, data: requests.post(url, data=data),
                   args=(provider_profile_original.return_url, nvp_dict)).start()

    set_counters(config)
    increment_history_field(config, 'orders_count_history')
    increment_history_field(config, 'items_traded_history', order.items_count)
    increment_history_field(config, 'turnover_history', provider_revenue)
    increment_history_field(config, 'earnings_history', provider_earnings)

    set_counters(customer)
    customer.last_payment_on = datetime.now()
    increment_history_field(customer, 'orders_count_history')
    increment_history_field(customer, 'items_purchased_history', order.items_count)
    increment_history_field(customer, 'turnover_history', provider_revenue)
    increment_history_field(customer, 'earnings_history', provider_earnings)

    # Test whether the referrer (of the current customer) is a Dara
    if dara:
        referrer_share_rate = dara.share_rate
        send_dara_notification_email(dara_service_original, order)

        set_counters(dara)
        dara.last_transaction_on = datetime.now()

        increment_history_field(dara, 'orders_count_history')
        increment_history_field(dara, 'items_traded_history', order.items_count)
        increment_history_field(dara, 'turnover_history', provider_revenue)
        increment_history_field(dara, 'earnings_history', provider_earnings)

        if dara_service_original:
            set_counters(dara_service_original)
            increment_history_field(dara_service_original, 'transaction_count_history')
            increment_history_field(dara_service_original, 'turnover_history', raw_provider_revenue)
            increment_history_field(dara_service_original, 'earnings_history', order.referrer_earnings)

        if dara_service_original:
            set_counters(provider_mirror)
            increment_history_field(provider_mirror, 'transaction_count_history')
            increment_history_field(provider_mirror, 'turnover_history', raw_provider_revenue)
            increment_history_field(provider_mirror, 'earnings_history', order.referrer_earnings)

        try:
            member_ref = Member.objects.using(referrer_db).get(pk=member.id)
        except Member.DoesNotExist:
            member.save(using=referrer_db)
            member_ref = Member.objects.using(referrer_db).get(pk=member.id)
            member.customer.save(using=referrer_db)
        customer_ref = member_ref.customer
        set_counters(customer_ref)
        customer_ref.last_payment_on = datetime.now()
        increment_history_field(customer_ref, 'orders_count_history')
        increment_history_field(customer_ref, 'items_purchased_history', order.items_count)
        increment_history_field(customer_ref, 'turnover_history', raw_provider_revenue)
        increment_history_field(customer_ref, 'earnings_history', order.retailer_earnings)

        dara_umbrella = Dara.objects.using(UMBRELLA).get(member=dara.member)
        if dara_umbrella.level == 1 and dara_umbrella.xp == 2:
            dara_umbrella.xp = 3
            dara_umbrella.raise_bonus_cash(200)
            dara_umbrella.save()

    category_list = []

    # Adding a 100 bonus in dara account to have buy online
    try:
        dara_as_buyer = Dara.objects.using(UMBRELLA).get(member=member)
        if dara_as_buyer.level == 1 and dara_as_buyer.xp == 0:
            dara_as_buyer.xp = 1
            dara_as_buyer.raise_bonus_cash(100)
            dara_as_buyer.save()
    except Dara.DoesNotExist:
        logging.error("The customer is not yet a Dara")

    for entry in order.entries:
        product = Product.objects.get(pk=entry.product.id)
        provider_service = product.provider
        provider_profile_umbrella = OperatorProfile.objects.using(UMBRELLA).get(service=provider_service)
        category = product.category

        turnover = entry.count * product.retail_price
        set_counters(category)
        provider_earnings = turnover * (100 - referrer_share_rate - provider_profile_umbrella.ikwen_share_rate) / 100
        increment_history_field(category, 'earnings_history', provider_earnings)
        increment_history_field(category, 'turnover_history', turnover)
        increment_history_field(category, 'items_traded_history', entry.count)
        if category not in category_list:
            increment_history_field(category, 'orders_count_history')
            category_list.append(category)

        if update_stock:
            product.stock -= entry.count
            if product.stock == 0:
                add_event(service, SOLD_OUT_EVENT, group_id=sudo_group.id, object_id=product.id)
                mark_duplicates(product)
        product.save()
        set_counters(product)
        increment_history_field(product, 'units_sold_history', entry.count)

    add_event(service, NEW_ORDER_EVENT, group_id=sudo_group.id, object_id=order.id)


def send_dara_notification_email(dara_service, order):
    service = get_service_instance()
    config = service.config
    template_name = 'playground/mails/new_transaction_test.html'

    activate(dara_service.member.language)
    subject = _("New transaction on Playground")
    try:
        dashboard_url = 'https://daraja.ikwen.com/daraja/dashboard/'
        html_content = get_mail_content(subject, template_name=template_name,
                                        extra_context={'currency_symbol': config.currency_symbol,
                                                       'amount': order.items_cost,
                                                       'dara_earnings': order.referrer_earnings,
                                                       'transaction_time': order.updated_on.strftime('%Y-%m-%d %H:%M:%S'),
                                                       'account_balance': dara_service.balance,
                                                       'dashboard_url': dashboard_url,
                                                       'dara': dara_service
                                                       })
        sender = 'Daraja Playground <no-reply@ikwen.com>'
        msg = EmailMessage(subject, html_content, sender, [dara_service.member.email])
        msg.content_subtype = "html"
        Thread(target=lambda m: m.send(), args=(msg,)).start()
    except:
        logger.error("Failed to notify %s Dara after follower purchase." % service, exc_info=True)


def send_order_confirmation_email(request, subject, buyer_name, buyer_email, dara, order, message=None,
                                  reward_pack_list=None):
    service = get_service_instance()
    coupon_count = 0
    if reward_pack_list:
        template_name = 'shopping/mails/order_notice_with_reward.html'
        for pack in reward_pack_list:
            coupon_count += pack.count
    else:
        template_name = 'playground/mails/order_notice.html'
    # invitation_url = 'https://daraja.ikwen.com/daraja/companies/'
    invitation_url = 'https://daraja.ikwen.com/'
    crcy = currencies(request)['CURRENCY']
    sender = 'Daraja Playground <no-reply@ikwen.com>'
    extra_context = {'buyer_name': buyer_name, 'order': order, 'message': message,
                     'IS_BANK': getattr(settings, 'IS_BANK', False),
                     'coupon_count': coupon_count, 'crcy': crcy, 'dara': dara}
    if dara:
        extra_context['invitation_url'] = invitation_url
    html_content = get_mail_content(subject, template_name=template_name, extra_context=extra_context)

    msg = XEmailMessage(subject, html_content, sender, [buyer_email])
    bcc = [email.strip() for email in service.config.notification_email.split(',') if email.strip()]
    bcc.append(service.member.email)
    msg.bcc = list(set(bcc))
    msg.content_subtype = "html"
    Thread(target=lambda m: m.send(), args=(msg,)).start()


def referee_registration_callback(request, *args, **kwargs):
    """
    This function should run upon registration. This is achieved
    by adding its path to the IKWEN_REGISTER_EVENTS in settings file.
    This does necessary operations to bind a Dara to the Member newly login in.
    """
    referrer = request.COOKIES.get('referrer')
    if referrer:
        try:
            service = kwargs.get('service', get_service_instance())
            dara_member = Member.objects.get(pk=referrer)
            set_customer_dara(service, dara_member, request.user)
        except:
            pass


def set_customer_dara(service, referrer, member):
    """
    Binds referrer to member referred.
    :param service: Referred Service
    :param referrer: Member who referred (The Dara)
    :param member: Referred Member
    :return:
    """
    try:
        db = service.database
        add_database(db)
        app = Application.objects.using(db).get(slug=DARAJA)
        dara_service = Service.objects.using(db).get(app=app, member=referrer)
        customer, change = Customer.objects.using(db).get_or_create(member=member)
        if customer.referrer:
            return

        dara_umbrella = Dara.objects.using(UMBRELLA).get(member=referrer)
        if dara_umbrella.level == 1 and dara_umbrella.xp == 1:
            dara_umbrella.xp = 2
            dara_umbrella.raise_bonus_cash(100)
            dara_umbrella.save()

        customer.referrer = dara_service
        customer.save()

        dara_db = dara_service.database
        add_database(dara_db)
        member.save(using=dara_db)
        customer.save(using=dara_db)
        service_mirror = Service.objects.using(dara_db).get(pk=service.id)
        set_counters(service_mirror)
        increment_history_field(service_mirror, 'community_history')

        add_event(service, REFEREE_JOINED_EVENT, member)

        diff = datetime.now() - member.date_joined

        activate(referrer.language)
        sender = "%s via Playground <no-reply@ikwen.com>" % member.full_name
        if diff.days > 1:
            subject = _("I'm back on %s !" % service.project_name)
        else:
            subject = _("I just joined %s !" % service.project_name)
        html_content = get_mail_content(subject, template_name='playground/mails/referee_joined.html',
                                        extra_context={'referred_service_name': service.project_name, 'referee': member,
                                                       'dara': dara_umbrella, 'cta_url': 'https://daraja.ikwen.com/'
                                                       })
        msg = EmailMessage(subject, html_content, sender, [referrer.email])
        msg.content_subtype = "html"
        Thread(target=lambda m: m.send(), args=(msg, )).start()
    except:
        logger.error("%s - Error while setting Customer Dara", exc_info=True)


class PlaygroundCart(Cart):

    def get_context_data(self, **kwargs):
        context = super(PlaygroundCart, self).get_context_data(**kwargs)
        try:
            context['dara'] = Dara.objects.get(member=self.request.user)
        except:
            pass
        return context
