# ~*~ coding: utf-8 ~*~
#

from __future__ import unicode_literals
import os
import datetime
from django.core.cache import cache
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.http import HttpResponse
from django.shortcuts import reverse, redirect
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic.base import TemplateView, RedirectView
from django.views.generic.edit import FormView
from django.conf import settings

from common.utils import get_request_ip, get_object_or_none
from users.models import User
from users.utils import (
    check_otp_code, is_block_login, clean_failed_count, get_user_or_tmp_user,
    set_tmp_user_to_cache, increase_login_failed_count,
    redirect_user_first_login_or_index
)
from ..models import LoginConfirmSetting
from ..signals import post_auth_success, post_auth_failed
from .. import forms
from .. import const


__all__ = [
    'UserLoginView', 'UserLoginOtpView', 'UserLogoutView',
    'UserLoginGuardView', 'UserLoginWaitConfirmView',
]


@method_decorator(sensitive_post_parameters(), name='dispatch')
@method_decorator(csrf_protect, name='dispatch')
@method_decorator(never_cache, name='dispatch')
class UserLoginView(FormView):
    form_class = forms.UserLoginForm
    form_class_captcha = forms.UserLoginCaptchaForm
    key_prefix_captcha = "_LOGIN_INVALID_{}"

    def get_template_names(self):
        template_name = 'authentication/login.html'
        if not settings.XPACK_ENABLED:
            return template_name

        from xpack.plugins.license.models import License
        if not License.has_valid_license():
            return template_name

        template_name = 'authentication/xpack_login.html'
        return template_name

    def get(self, request, *args, **kwargs):
        if request.user.is_staff:
            return redirect(redirect_user_first_login_or_index(
                request, self.redirect_field_name)
            )
        # show jumpserver login page if request http://{JUMP-SERVER}/?admin=1
        if settings.AUTH_OPENID and not self.request.GET.get('admin', 0):
            query_string = request.GET.urlencode()
            login_url = "{}?{}".format(settings.LOGIN_URL, query_string)
            return redirect(login_url)
        request.session.set_test_cookie()
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # limit login authentication
        ip = get_request_ip(request)
        username = self.request.POST.get('username')
        if is_block_login(username, ip):
            return self.render_to_response(self.get_context_data(block_login=True))
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        if not self.request.session.test_cookie_worked():
            return HttpResponse(_("Please enable cookies and try again."))
        user = form.get_user()
        # user password expired
        if user.password_has_expired:
            reason = const.password_expired
            self.send_auth_signal(success=False, username=user.username, reason=reason)
            return self.render_to_response(self.get_context_data(password_expired=True))

        set_tmp_user_to_cache(self.request, user)
        username = form.cleaned_data.get('username')
        ip = get_request_ip(self.request)
        # 登陆成功，清除缓存计数
        clean_failed_count(username, ip)
        self.request.session['auth_password'] = '1'
        return self.redirect_to_guard_view()

    def form_invalid(self, form):
        # write login failed log
        username = form.cleaned_data.get('username')
        exist = User.objects.filter(username=username).first()
        reason = const.password_failed if exist else const.user_not_exist
        # limit user login failed count
        ip = get_request_ip(self.request)
        increase_login_failed_count(username, ip)
        form.add_limit_login_error(username, ip)
        # show captcha
        cache.set(self.key_prefix_captcha.format(ip), 1, 3600)
        self.send_auth_signal(success=False, username=username, reason=reason)

        old_form = form
        form = self.form_class_captcha(data=form.data)
        form._errors = old_form.errors
        return super().form_invalid(form)

    @staticmethod
    def redirect_to_guard_view():
        continue_url = reverse('authentication:login-guard')
        return redirect(continue_url)

    def get_form_class(self):
        ip = get_request_ip(self.request)
        if cache.get(self.key_prefix_captcha.format(ip)):
            return self.form_class_captcha
        else:
            return self.form_class

    def get_context_data(self, **kwargs):
        context = {
            'demo_mode': os.environ.get("DEMO_MODE"),
            'AUTH_OPENID': settings.AUTH_OPENID,
        }
        kwargs.update(context)
        return super().get_context_data(**kwargs)


class UserLoginOtpView(FormView):
    template_name = 'authentication/login_otp.html'
    form_class = forms.UserCheckOtpCodeForm
    redirect_field_name = 'next'

    def form_valid(self, form):
        user = get_user_or_tmp_user(self.request)
        otp_code = form.cleaned_data.get('otp_code')
        otp_secret_key = user.otp_secret_key

        if check_otp_code(otp_secret_key, otp_code):
            self.request.session['auth_otp'] = '1'
            return UserLoginView.redirect_to_guard_view()
        else:
            self.send_auth_signal(
                success=False, username=user.username,
                reason=const.mfa_failed
            )
            form.add_error(
                'otp_code', _('MFA code invalid, or ntp sync server time')
            )
            return super().form_invalid(form)

    def send_auth_signal(self, success=True, user=None, username='', reason=''):
        if success:
            post_auth_success.send(sender=self.__class__, user=user, request=self.request)
        else:
            post_auth_failed.send(
                sender=self.__class__, username=username,
                request=self.request, reason=reason
            )


class UserLoginGuardView(RedirectView):
    redirect_field_name = 'next'

    def get_redirect_url(self, *args, **kwargs):
        if not self.request.session.get('auth_password'):
            return reverse('authentication:login')

        user = get_user_or_tmp_user(self.request)
        # 启用并设置了otp
        if user.otp_enabled and user.otp_secret_key and \
                not self.request.session.get('auth_otp'):
            return reverse('authentication:login-otp')
        confirm_setting = user.get_login_confirm_setting()
        if confirm_setting and not self.request.session.get('auth_confirm'):
            order = confirm_setting.create_confirm_order(self.request)
            self.request.session['auth_order_id'] = str(order.id)
            url = reverse('authentication:login-wait-confirm')
            return url
        self.login_success(user)
        # 启用但是没有设置otp
        if user.otp_enabled and not user.otp_secret_key:
            # 1,2,mfa_setting & F
            return reverse('users:user-otp-enable-authentication')
        url = redirect_user_first_login_or_index(
            self.request, self.redirect_field_name
        )
        return url

    def login_success(self, user):
        auth_login(self.request, user)
        self.send_auth_signal(success=True, user=user)

    def send_auth_signal(self, success=True, user=None, username='', reason=''):
        if success:
            post_auth_success.send(sender=self.__class__, user=user, request=self.request)
        else:
            post_auth_failed.send(
                sender=self.__class__, username=username,
                request=self.request, reason=reason
            )


class UserLoginWaitConfirmView(TemplateView):
    template_name = 'authentication/login_wait_confirm.html'

    def get_context_data(self, **kwargs):
        from orders.models import LoginConfirmOrder
        order_id = self.request.session.get("auth_order_id")
        if not order_id:
            order = None
        else:
            order = get_object_or_none(LoginConfirmOrder, pk=order_id)
        context = super().get_context_data(**kwargs)
        if order:
            order_detail_url = reverse('orders:login-confirm-order-detail', kwargs={'pk': order_id})
            timestamp_created = datetime.datetime.timestamp(order.date_created)
            msg = _("""Wait for <b>{}</b> confirm, You also can copy link to her/him <br/>
                  Don't close this page""").format(order.assignees_display)
        else:
            timestamp_created = 0
            order_detail_url = ''
            msg = _("No order found")
        context.update({
            "msg": msg,
            "timestamp": timestamp_created,
            "order_detail_url": order_detail_url
        })
        return context


@method_decorator(never_cache, name='dispatch')
class UserLogoutView(TemplateView):
    template_name = 'flash_message_standalone.html'

    def get(self, request, *args, **kwargs):
        auth_logout(request)
        next_uri = request.COOKIES.get("next")
        if next_uri:
            return redirect(next_uri)
        response = super().get(request, *args, **kwargs)
        return response

    def get_context_data(self, **kwargs):
        context = {
            'title': _('Logout success'),
            'messages': _('Logout success, return login page'),
            'interval': 1,
            'redirect_url': reverse('authentication:login'),
            'auto_redirect': True,
        }
        kwargs.update(context)
        return super().get_context_data(**kwargs)



