from django.shortcuts import render, get_object_or_404, redirect
from django.views.generic import TemplateView, ListView, DetailView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth import login
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Count, Case, When, IntegerField
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from .models import (
    Voucher, Particular, VoucherApproval, Designation,
    ApprovalLevel, UserProfile, AccountDetail, CompanyDetail
)
from .serializers import VoucherSerializer, VoucherApprovalSerializer, AccountDetailSerializer
from django.contrib.auth.models import User, Group
from django.contrib.auth.hashers import make_password
from django.db import transaction, OperationalError
from django.db.models import F
from decimal import Decimal, InvalidOperation
import time
from datetime import datetime


# === MIXINS ===
class AccountantRequiredMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            if request.headers.get('Accept') == 'application/json' or request.path.startswith('/api/'):
                return JsonResponse({'error': 'Authentication required.'}, status=401)
            return self.handle_no_permission()
        
        if not request.user.groups.filter(name='Accountants').exists():
            error_msg = "Only Accountants can perform this action."
            if request.headers.get('Accept') == 'application/json' or request.path.startswith('/api/'):
                return JsonResponse({'error': error_msg}, status=403)
            messages.error(request, error_msg)
            return redirect('home')
        return super().dispatch(request, *args, **kwargs)


class AdminStaffRequiredMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            if request.headers.get('Accept') == 'application/json' or request.path.startswith('/api/'):
                return JsonResponse({'error': 'Authentication required.'}, status=401)
            return self.handle_no_permission()
        
        if not (request.user.groups.filter(name='Admin Staff').exists() or request.user.is_superuser):
            error_msg = "Only Admin Staff can perform this action."
            if request.headers.get('Accept') == 'application/json' or request.path.startswith('/api/'):
                return JsonResponse({'error': error_msg}, status=403)
            messages.error(request, error_msg)
            return redirect('home')
        return super().dispatch(request, *args, **kwargs)


# === VIEWS ===
class HomeView(TemplateView):
    template_name = 'vouchers/home.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        context['can_create_voucher'] = user.is_authenticated
        context['is_admin_staff'] = user.is_authenticated and (user.groups.filter(name='Admin Staff').exists() or user.is_superuser)
        context['is_superuser'] = user.is_superuser
        context['designations'] = Designation.objects.all()

        if user.is_superuser:
            context['all_users'] = User.objects.select_related('userprofile__designation').all()
            company = CompanyDetail.load()
            context['company'] = company
            # ADDED: Pass company logo URL for dashboard icon
            context['company_logo_url'] = company.logo.url if company.logo else None

        return context


class VoucherListView(LoginRequiredMixin, ListView):
    model = Voucher
    template_name = 'vouchers/voucher_list.html'
    context_object_name = 'vouchers'
    paginate_by = 10

    def get_queryset(self):
        qs = super().get_queryset().select_related('created_by')
        qs = qs.prefetch_related('particulars', 'approvals', 'approvals__approver')
        return qs.annotate(
            approved_count=Count(Case(When(approvals__status='APPROVED', then=1)), output_field=IntegerField()),
            rejected_count=Count(Case(When(approvals__status='REJECTED', then=1)), output_field=IntegerField())
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context['can_create_voucher'] = user.is_authenticated
        context['is_admin_staff'] = user.is_authenticated and (user.groups.filter(name='Admin Staff').exists() or user.is_superuser)
        context['designations'] = Designation.objects.all()

        for voucher in context['vouchers']:
            try:
                voucher.user_approval = voucher.approvals.get(approver=user)
            except VoucherApproval.DoesNotExist:
                voucher.user_approval = None

            # === REQUIRED APPROVERS (SNAPSHOT) ===
            required_snapshot = voucher.required_approvers_snapshot or []
            approved_usernames = set(
                voucher.approvals.filter(status='APPROVED')
                .values_list('approver__username', flat=True)
            )

            # For PENDING: show dynamic levels
            if voucher.status == 'PENDING':
                required = set(voucher.required_approvers)
                voucher.pending_approvers = [
                    {'name': name, 'has_approved': name in approved_usernames}
                    for name in required
                ]
            else:
                voucher.pending_approvers = [
                    {'name': name, 'has_approved': True}
                    for name in required_snapshot
                    if name in approved_usernames
                ]

            # === APPROVAL LEVELS PROGRESS ===
            if voucher.status == 'PENDING':
                levels = ApprovalLevel.objects.filter(is_active=True) \
                    .select_related('designation').order_by('order')
                level_data = []
                for level in levels:
                    level_users = UserProfile.objects.filter(
                        designation=level.designation,
                        user__groups__name='Admin Staff'
                    ).values_list('user__username', flat=True)
                    all_approved = all(u in approved_usernames for u in level_users)
                    some_approved = any(u in approved_usernames for u in level_users)
                    level_data.append({
                        'designation': level.designation,
                        'all_approved': all_approved,
                        'some_approved': some_approved,
                        'is_next': False
                    })
                for lvl in level_data:
                    if not lvl['all_approved']:
                        lvl['is_next'] = True
                        break
                voucher.approval_levels = level_data
            else:
                level_data = [
                    {
                        'designation': {'name': name},
                        'all_approved': True,
                        'some_approved': True,
                        'is_next': False
                    }
                    for name in required_snapshot
                    if name in approved_usernames
                ]
                voucher.approval_levels = level_data

            # === CAN APPROVE & WAITING FOR ===
            can_approve = False
            waiting_for_username = None

            if voucher.status == 'PENDING':
                first_pending_level = None
                levels = ApprovalLevel.objects.filter(is_active=True).order_by('order')
                for lvl in levels:
                    level_users = UserProfile.objects.filter(
                        designation=lvl.designation,
                        user__groups__name='Admin Staff',
                        user__is_active=True
                    ).values_list('user__id', flat=True)
                    approved_in_level = voucher.approvals.filter(
                        status='APPROVED',
                        approver__id__in=level_users
                    ).count()
                    if approved_in_level < len(level_users):
                        first_pending_level = lvl
                        break

                if first_pending_level:
                    pending_users = UserProfile.objects.filter(
                        designation=first_pending_level.designation,
                        user__groups__name='Admin Staff',
                        user__is_active=True
                    ).exclude(
                        id__in=voucher.approvals.filter(status='APPROVED').values_list('approver__id', flat=True)
                    ).values_list('user__username', flat=True)
                    waiting_for_username = ", ".join(pending_users) if pending_users else "next level"
                else:
                    waiting_for_username = "Approved"
            else:
                waiting_for_username = "Approved"

            if voucher.status == 'PENDING' and (user.groups.filter(name='Admin Staff').exists() or user.is_superuser):
                current_level = None
                for lvl in levels:
                    users_in_level = UserProfile.objects.filter(
                        designation=lvl.designation,
                        user__groups__name='Admin Staff',
                        user__is_active=True
                    ).values_list('user__username', flat=True)
                    if user.username in users_in_level:
                        current_level = lvl
                        break
                if current_level and first_pending_level == current_level:
                    can_approve = True

            voucher.can_approve = can_approve
            voucher.waiting_for_username = waiting_for_username

        return context


class VoucherDetailView(LoginRequiredMixin, DetailView):
    model = Voucher
    template_name = 'vouchers/voucher_detail.html'
    context_object_name = 'voucher'

    def get_queryset(self):
        qs = super().get_queryset().select_related('created_by') \
            .prefetch_related('particulars', 'approvals__approver')
        return qs.annotate(
            approved_count=Count(Case(When(approvals__status='APPROVED', then=1)), output_field=IntegerField()),
            rejected_count=Count(Case(When(approvals__status='REJECTED', then=1)), output_field=IntegerField())
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        voucher = context['voucher']

        context['can_create_voucher'] = user.is_authenticated
        context['is_admin_staff'] = user.is_authenticated and (user.groups.filter(name='Admin Staff').exists() or user.is_superuser)
        context['designations'] = Designation.objects.all()

        try:
            context['user_approval'] = voucher.approvals.get(approver=user)
        except VoucherApproval.DoesNotExist:
            context['user_approval'] = None

        total = len(voucher.required_approvers)
        approved = getattr(voucher, 'approved_count', 0) or 0
        context['approval_percentage'] = (approved / total * 100) if total > 0 else 100

        approved_usernames = set(
            voucher.approvals.filter(status='APPROVED')
            .values_list('approver__username', flat=True)
        )

        if voucher.status == 'APPROVED':
            snapshot = voucher.required_approvers_snapshot or []
            context['pending_approvers'] = [
                {'name': name, 'has_approved': True}
                for name in snapshot
                if name in approved_usernames
            ]
        else:
            required = set(voucher.required_approvers)
            context['pending_approvers'] = [
                {'name': name, 'has_approved': name in approved_usernames}
                for name in required
            ]

        if voucher.status == 'PENDING':
            levels = ApprovalLevel.objects.filter(is_active=True) \
                .select_related('designation').order_by('order')
            level_data = []
            for level in levels:
                level_users = UserProfile.objects.filter(
                    designation=level.designation,
                    user__groups__name='Admin Staff'
                ).values_list('user__username', flat=True)
                all_approved = all(u in approved_usernames for u in level_users)
                some_approved = any(u in approved_usernames for u in level_users)
                level_data.append({
                    'designation': level.designation,
                    'all_approved': all_approved,
                    'some_approved': some_approved,
                    'is_next': False
                })
            for lvl in level_data:
                if not lvl['all_approved']:
                    lvl['is_next'] = True
                    break
            context['approval_levels'] = level_data
        else:
            snapshot = voucher.required_approvers_snapshot or []
            context['approval_levels'] = [
                {
                    'designation': {'name': name},
                    'all_approved': True,
                    'some_approved': True,
                    'is_next': False
                }
                for name in snapshot
                if name in approved_usernames
            ]

        can_approve = False
        waiting_for_username = None

        if voucher.status == 'PENDING':
            first_pending_level = None
            levels = ApprovalLevel.objects.filter(is_active=True).order_by('order')
            for lvl in levels:
                level_users = UserProfile.objects.filter(
                    designation=lvl.designation,
                    user__groups__name='Admin Staff',
                    user__is_active=True
                ).values_list('user__id', flat=True)
                approved_in_level = voucher.approvals.filter(
                    status='APPROVED',
                    approver__id__in=level_users
                ).count()
                if approved_in_level < len(level_users):
                    first_pending_level = lvl
                    break

            if first_pending_level:
                pending_users = UserProfile.objects.filter(
                    designation=first_pending_level.designation,
                    user__groups__name='Admin Staff',
                    user__is_active=True
                ).exclude(
                    id__in=voucher.approvals.filter(status='APPROVED').values_list('approver__id', flat=True)
                ).values_list('user__username', flat=True)
                waiting_for_username = ", ".join(pending_users) if pending_users else "next level"
            else:
                waiting_for_username = "Approved"
        else:
            waiting_for_username = "Approved"

        if voucher.status == 'PENDING' and (user.groups.filter(name='Admin Staff').exists() or user.is_superuser):
            current_level = None
            for lvl in levels:
                users_in_level = UserProfile.objects.filter(
                    designation=lvl.designation,
                    user__groups__name='Admin Staff',
                    user__is_active=True
                ).values_list('user__username', flat=True)
                if user.username in users_in_level:
                    current_level = lvl
                    break
            if current_level and first_pending_level == current_level:
                can_approve = True

        context['can_approve'] = can_approve
        context['waiting_for_username'] = waiting_for_username
        context['user_profile'] = user.userprofile if hasattr(user, 'userprofile') else None
        context['company'] = CompanyDetail.load()

        return context


# === FULLY FIXED VoucherCreateAPI – NOW SUPPORTS BOTH CREATE & EDIT ===
class VoucherCreateAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.POST.copy()
        files = request.FILES

        # === Check if this is an EDIT (voucher_id sent from frontend) ===
        voucher_id = data.get('voucher_id') or request.data.get('voucher_id')
        is_edit = bool(voucher_id)

        particulars = []
        i = 0
        while f'particulars[{i}][description]' in data:
            desc = data.get(f'particulars[{i}][description]', '').strip()
            amt = data.get(f'particulars[{i}][amount]', '').strip()
            file_key = f'particulars[{i}][attachment]'
            attachment = files.get(file_key)

            if desc and amt:
                try:
                    amount = Decimal(amt)
                    if amount <= 0:
                        return Response(
                            {'particulars': f'Amount must be > 0 for item {i+1}'},
                            status=status.HTTP_400_BAD_REQUEST
                        )
                except InvalidOperation:
                    return Response(
                        {'particulars': f'Invalid amount for item {i+1}'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                if not attachment and not is_edit:  # Allow skipping attachment on edit
                    return Response(
                        {'particulars': f'Attachment is required for particular {i+1}'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                particulars.append({
                    'description': desc,
                    'amount': amount,
                    'attachment': attachment
                })
            i += 1

        if not particulars:
            return Response(
                {'particulars': 'At least one particular is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        payment_type = data.get('payment_type')
        cheque_number = None
        cheque_date = None
        cheque_attachment = None
        account_details_id = None

        if payment_type == 'CHEQUE':
            cheque_number = data.get('cheque_number', '').strip()
            cheque_date_str = data.get('cheque_date', '').strip()
            cheque_attachment = files.get('cheque_attachment')
            account_details_id = data.get('account_details', '').strip()

            if not cheque_number:
                return Response({'error': 'Cheque number is required.'}, status=400)
            if not cheque_date_str:
                return Response({'error': 'Cheque date is required for Cheque payments.'}, status=400)
            if not cheque_attachment and not is_edit:
                return Response({'error': 'Cheque attachment is required.'}, status=400)
            if not account_details_id:
                return Response({'error': 'Account Details is required for Cheque payments.'}, status=400)

            try:
                cheque_date = datetime.strptime(cheque_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid cheque date format. Use YYYY-MM-DD.'}, status=400)

            try:
                AccountDetail.objects.get(pk=account_details_id)
            except AccountDetail.DoesNotExist:
                return Response({'error': 'Invalid account selected.'}, status=400)
        else:
            data.pop('cheque_number', None)
            data.pop('cheque_date', None)
            data.pop('cheque_attachment', None)
            data.pop('account_details', None)

        serializer_data = {
            'voucher_date': data.get('voucher_date'),
            'payment_type': payment_type,
            'name_title': data.get('name_title'),
            'pay_to': data.get('pay_to'),
            'cheque_number': cheque_number,
            'cheque_date': cheque_date,
            'account_details': account_details_id if account_details_id else None,
            'attachment': files.get('attachment'),
            'particulars': particulars
        }

        if payment_type == 'CHEQUE':
            serializer_data['cheque_attachment'] = cheque_attachment

        # === EDIT MODE: Update existing voucher ===
        if is_edit:
            try:
                voucher = Voucher.objects.get(
                    id=voucher_id,
                    created_by=request.user,
                    status='PENDING',
                    approvals__isnull=True  # No approvals yet
                )
            except Voucher.DoesNotExist:
                return Response(
                    {'error': 'Voucher not found or cannot be edited (already approved/rejected).'},
                    status=status.HTTP_404_NOT_FOUND
                )
            serializer = VoucherSerializer(voucher, data=serializer_data, partial=True, context={'request': request, 'files': files})
        else:
            serializer = VoucherSerializer(data=serializer_data, context={'request': request, 'files': files})

        if serializer.is_valid():
            voucher = serializer.save(created_by=request.user if not is_edit else voucher.created_by)
            action = "updated" if is_edit else "created"
            return Response({
                'success': True,
                'message': f'Voucher {voucher.voucher_number} {action} successfully!',
                'voucher': VoucherSerializer(voucher, context={'request': request}).data
            }, status=status.HTTP_200_OK if is_edit else status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# === ALL OTHER VIEWS REMAIN 100% UNCHANGED ===
class AccountDetailListAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        accounts = AccountDetail.objects.all().order_by('bank_name')
        serializer = AccountDetailSerializer(accounts, many=True)
        return Response(serializer.data)


class AccountDetailCreateAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=403)

        bank_name = request.data.get('bank_name', '').strip()
        account_number = request.data.get('account_number', '').strip()

        if not bank_name or not account_number:
            return Response({'error': 'Bank name and account number are required'}, status=400)

        if AccountDetail.objects.filter(bank_name=bank_name, account_number=account_number).exists():
            return Response({'error': 'This account already exists'}, status=400)

        account = AccountDetail.objects.create(
            bank_name=bank_name,
            account_number=account_number,
            created_by=request.user
        )
        return Response({
            'id': account.id,
            'label': str(account)
        }, status=201)


class AccountDetailDeleteAPI(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=403)

        try:
            account = AccountDetail.objects.get(pk=pk)
            account.delete()
            return Response({'message': 'Account deleted successfully'}, status=200)
        except AccountDetail.DoesNotExist:
            return Response({'error': 'Account not found'}, status=404)


class VoucherApprovalAPI(AdminStaffRequiredMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        status_choice = request.data.get('status')
        rejection_reason = request.data.get('rejection_reason', '').strip()

        if status_choice not in ['APPROVED', 'REJECTED']:
            return Response({'status': ['Invalid choice.']}, status=status.HTTP_400_BAD_REQUEST)

        if status_choice == 'REJECTED' and not rejection_reason:
            return Response(
                {'rejection_reason': 'Reason is required when rejecting.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                with transaction.atomic():
                    voucher = Voucher.objects.select_for_update(nowait=True).get(pk=pk)

                    if request.user.username not in voucher.required_approvers:
                        return Response(
                            {'error': 'You are not authorized to approve this voucher.'},
                            status=status.HTTP_403_FORBIDDEN
                        )

                    if voucher.status != 'PENDING':
                        return Response(
                            {'error': 'This voucher is no longer pending.'},
                            status=status.HTTP_400_BAD_REQUEST
                        )

                    levels = ApprovalLevel.objects.filter(is_active=True).order_by('order')
                    approved_usernames = set(
                        voucher.approvals.filter(status='APPROVED')
                        .values_list('approver__username', flat=True)
                    )

                    current_user_level = None
                    for level in levels:
                        users_in_level = UserProfile.objects.filter(
                            designation=level.designation,
                            user__groups__name='Admin Staff'
                        ).values_list('user__username', flat=True)
                        if request.user.username in users_in_level:
                            current_user_level = level
                            break

                    if not current_user_level:
                        return Response(
                            {'error': 'Your designation is not in the approval chain.'},
                            status=status.HTTP_403_FORBIDDEN
                        )

                    prev_level = ApprovalLevel.objects.filter(
                        order__lt=current_user_level.order, is_active=True
                    ).order_by('-order').first()

                    can_approve = True
                    waiting_for = None
                    if prev_level:
                        prev_users = UserProfile.objects.filter(
                            designation=prev_level.designation,
                            user__groups__name='Admin Staff'
                        ).values_list('user__username', flat=True)
                        if not all(u in approved_usernames for u in prev_users):
                            can_approve = False
                            waiting_for = prev_level.designation.name

                    if not can_approve:
                        return Response({
                            'error': f'Waiting for {waiting_for} to approve first.',
                            'can_approve': False,
                            'waiting_for': waiting_for
                        }, status=status.HTTP_403_FORBIDDEN)

                    approval, created = VoucherApproval.objects.update_or_create(
                        voucher=voucher,
                        approver=request.user,
                        defaults={
                            'status': status_choice,
                            'rejection_reason': rejection_reason if status_choice == 'REJECTED' else None
                        }
                    )

                    voucher.refresh_from_db()
                    voucher._update_status_if_all_approved()

                serializer = VoucherSerializer(voucher, context={'request': request})
                response_data = serializer.data
                response_data['status'] = status_choice
                response_data['approval'] = {
                    'approver': request.user.username,
                    'approved_at': approval.approved_at.strftime('%d %b %H:%M'),
                    'rejection_reason': approval.rejection_reason
                }
                response_data['can_approve'] = True
                return Response(response_data, status=status.HTTP_200_OK)

            except OperationalError as e:
                if 'database is locked' in str(e).lower() and attempt < max_retries - 1:
                    time.sleep(0.1 * (2 ** attempt))
                else:
                    return Response(
                        {'error': 'Database is busy. Please try again in a moment.'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE
                    )
            except Voucher.DoesNotExist:
                return Response({'error': 'Voucher not found.'}, status=404)

        return Response(
            {'error': 'Failed to process approval due to database lock.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )


# === ALL OTHER APIs BELOW ARE 100% UNCHANGED ===
class DesignationCreateAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=status.HTTP_403_FORBIDDEN)

        name = request.data.get('name', '').strip()
        if not name:
            return Response({'error': 'Name is required'}, status=status.HTTP_400_BAD_REQUEST)

        if Designation.objects.filter(name=name).exists():
            return Response({'error': 'Designation already exists'}, status=status.HTTP_400_BAD_REQUEST)

        designation = Designation.objects.create(name=name, created_by=request.user)
        return Response({
            'message': f"Designation '{designation.name}' created.",
            'id': designation.id
        }, status=status.HTTP_201_CREATED)


class ApprovalControlAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=status.HTTP_403_FORBIDDEN)

        levels = ApprovalLevel.objects.select_related('designation').order_by('order')
        return Response({
            'levels': [
                {
                    'id': l.designation.id,
                    'name': l.designation.name,
                    'order': l.order,
                    'is_active': l.is_active
                }
                for l in levels
            ],
            'all_designations': list(Designation.objects.values('id', 'name'))
        })

    def post(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=status.HTTP_403_FORBIDDEN)

        levels_data = request.data.get('levels', [])
        if not isinstance(levels_data, list):
            return Response({'error': 'levels must be a list'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            ApprovalLevel.objects.all().delete()
            for idx, item in enumerate(levels_data):
                des_id = item.get('id')
                is_active = item.get('is_active', True)
                if not des_id:
                    continue
                try:
                    des = Designation.objects.get(id=des_id)
                    ApprovalLevel.objects.create(
                        designation=des,
                        order=idx + 1,
                        is_active=is_active,
                        updated_by=request.user
                    )
                except Designation.DoesNotExist:
                    pass

        return Response({'message': 'Approval order saved.'}, status=status.HTTP_200_OK)


class UserCreateAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=status.HTTP_403_FORBIDDEN)

        username = request.data.get('username', '').strip()
        password = request.data.get('password', '')
        user_group = request.data.get('user_group', '')
        designation_id = request.data.get('designation')
        signature = request.FILES.get('signature')

        if not username or not password or not user_group:
            return Response({'error': 'Username, password, and group are required'}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(username=username).exists():
            return Response({'error': 'Username already exists'}, status=status.HTTP_400_BAD_REQUEST)
        if user_group not in ['Accountants', 'Admin Staff']:
            return Response({'error': 'Invalid group'}, status=status.HTTP_400_BAD_REQUEST)
        if user_group == 'Admin Staff' and not designation_id:
            return Response({'error': 'Designation is required for Admin Staff'}, status=status.HTTP_400_BAD_REQUEST)
        if len(password) < 8:
            return Response({'error': 'Password must be at least 8 characters'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.create(username=username, password=make_password(password))
            group = Group.objects.get(name=user_group)
            user.groups.add(group)

            profile = UserProfile.objects.create(user=user)
            if user_group == 'Admin Staff':
                designation = Designation.objects.get(id=designation_id)
                profile.designation = designation
            if signature:
                profile.signature = signature
            profile.save()

            return Response({
                'message': f'User "{username}" created successfully.',
                'id': user.id
            }, status=status.HTTP_201_CREATED)

        except Group.DoesNotExist:
            return Response({'error': 'Group does not exist'}, status=status.HTTP_400_BAD_REQUEST)
        except Designation.DoesNotExist:
            return Response({'error': 'Invalid designation'}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class VoucherDeleteAPI(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=status.HTTP_403_FORBIDDEN)

        voucher = get_object_or_404(Voucher, pk=pk)
        voucher_number = voucher.voucher_number
        voucher.delete()

        return Response({
            'message': f'Voucher {voucher_number} deleted successfully.'
        }, status=status.HTTP_200_OK)


class UserUpdateAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=status.HTTP_403_FORBIDDEN)

        user_id = request.data.get('user_id')
        username = request.data.get('username', '').strip()
        group_name = request.data.get('user_group')
        designation_id = request.data.get('designation')
        is_active = request.data.get('is_active') in [True, 'true', 'True']
        signature = request.FILES.get('signature')

        if not user_id or not group_name or not username:
            return Response({'error': 'Missing required fields'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        if username != user.username and User.objects.filter(username=username).exists():
            return Response({'error': 'Username already taken'}, status=status.HTTP_400_BAD_REQUEST)

        user.username = username
        user.is_active = is_active
        user.save()

        user.groups.clear()
        try:
            group = Group.objects.get(name=group_name)
            user.groups.add(group)
        except Group.DoesNotExist:
            return Response({'error': 'Invalid group'}, status=status.HTTP_400_BAD_REQUEST)

        profile, _ = UserProfile.objects.get_or_create(user=user)
        if group_name == 'Admin Staff':
            if not designation_id:
                return Response({'error': 'Designation required for Admin Staff'}, status=status.HTTP_400_BAD_REQUEST)
            try:
                designation = Designation.objects.get(id=designation_id)
                profile.designation = designation
            except Designation.DoesNotExist:
                return Response({'error': 'Invalid designation'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            profile.designation = None

        if signature:
            profile.signature = signature

        profile.save()

        return Response({'message': 'User updated successfully'}, status=status.HTTP_200_OK)


class CompanyDetailAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=403)
        company = CompanyDetail.load()
        from .serializers import CompanyDetailSerializer
        serializer = CompanyDetailSerializer(company, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=403)

        company = CompanyDetail.load()
        data = request.POST.copy()
        files = request.FILES

        company.name = data.get('name', company.name).strip()
        company.gst_no = data.get('gst_no', company.gst_no or '').strip()
        company.pan_no = data.get('pan_no', company.pan_no or '').strip()
        company.address = data.get('address', company.address or '').strip()
        company.email = data.get('email', company.email or '').strip()
        company.phone = data.get('phone', company.phone or '').strip()

        if 'logo' in files:
            company.logo = files['logo']

        company.updated_by = request.user
        company.save()

        from .serializers import CompanyDetailSerializer
        serializer = CompanyDetailSerializer(company, context={'request': request})
        return Response({
            'message': 'Company details saved successfully.',
            'company': serializer.data
        })