# coding:utf-8
#
from django.urls import path
from rest_framework.routers import DefaultRouter

from .. import api

app_name = 'authentication'
router = DefaultRouter()
router.register('access-keys', api.AccessKeyViewSet, 'access-key')


urlpatterns = [
    # path('token/', api.UserToken.as_view(), name='user-token'),
    path('auth/', api.UserAuthApi.as_view(), name='user-auth'),
    path('tokens/', api.TokenCreateApi.as_view(), name='auth-token'),
    path('mfa/challenge/', api.MFAChallengeApi.as_view(), name='mfa-challenge'),
    path('connection-token/',
         api.UserConnectionTokenApi.as_view(), name='connection-token'),
    path('otp/auth/', api.UserOtpAuthApi.as_view(), name='user-otp-auth'),
    path('otp/verify/', api.UserOtpVerifyApi.as_view(), name='user-otp-verify'),
    path('order/auth/', api.UserOrderAcceptAuthApi.as_view(), name='user-order-auth'),
    path('login-confirm-settings/<uuid:user_id>/', api.LoginConfirmSettingUpdateApi.as_view(), name='login-confirm-setting-update')
]

urlpatterns += router.urls

