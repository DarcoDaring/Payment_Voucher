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
    ApprovalLevel, UserProfile
)
from .serializers import VoucherSerializer, VoucherApprovalSerializer
from django.contrib.auth.models import User, Group
from django.contrib.auth.hashers import make_password
from django.db import transaction, OperationalError
from django.db.models import F
from decimal import Decimal, InvalidOperation
import time


# === MIXINS ===
class AccountantRequiredMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not request.user.groups.filter(name='Accountants').exists():
            messages.error(request, "Only Accountants can perform this action.")
            return redirect('home')
        return super().dispatch(request, *args, **kwargs)


class AdminStaffRequiredMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not (request.user.groups.filter(name='Admin Staff').exists() or request.user.is_superuser):
            messages.error(request, "Only Admin Staff can perform this action.")
            return redirect('home')
        return super().dispatch(request, *args, **kwargs)


# === VIEWS ===
class HomeView(TemplateView):
    template_name = 'vouchers/home.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        context['is_accountant'] = user.is_authenticated and user.groups.filter(name='Accountants').exists()
        context['is_admin_staff'] = user.is_authenticated and (user.groups.filter(name='Admin Staff').exists() or user.is_superuser)
        context['is_superuser'] = user.is_superuser
        context['designations'] = Designation.objects.all()
        return context


class VoucherListView(LoginRequiredMixin, ListView):
    model = Voucher
    template_name = 'vouchers/voucher_list.html'
    context_object_name = 'vouchers'
    paginate_by = 10

    def get_queryset(self):
        qs = super().get_queryset().select_related('created_by')
        qs = qs.prefetch_related('particulars', 'approvals', 'approvals__approver')
        if not self.request.user.is_superuser:
            if self.request.user.groups.filter(name='Accountants').exists():
                qs = qs.filter(created_by=self.request.user)
        return qs.annotate(
            approved_count=Count(Case(When(approvals__status='APPROVED', then=1)), output_field=IntegerField()),
            rejected_count=Count(Case(When(approvals__status='REJECTED', then=1)), output_field=IntegerField())
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context['is_accountant'] = user.is_authenticated and user.groups.filter(name='Accountants').exists()
        context['is_admin_staff'] = user.is_authenticated and (user.groups.filter(name='Admin Staff').exists() or user.is_superuser)
        context['designations'] = Designation.objects.all()

        # === PROCESS EACH VOUCHER ===
        for voucher in context['vouchers']:
            # User approval
            try:
                voucher.user_approval = voucher.approvals.get(approver=user)
            except VoucherApproval.DoesNotExist:
                voucher.user_approval = None

            # Required approvers (snapshot or dynamic)
            required = set(voucher.required_approvers)
            approved_usernames = set(
                voucher.approvals.filter(status='APPROVED')
                .values_list('approver__username', flat=True)
            )
            voucher.pending_approvers = [
                {'name': name, 'has_approved': name in approved_usernames}
                for name in required
            ]

            # === SEQUENTIAL APPROVAL PROGRESS (for list view) ===
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
                # For APPROVED/REJECTED: show snapshot-based progress
                snapshot_users = set(voucher.required_approvers_snapshot or [])
                level_data = []
                for name in snapshot_users:
                    level_data.append({
                        'designation': {'name': name},
                        'all_approved': name in approved_usernames,
                        'some_approved': name in approved_usernames,
                        'is_next': False
                    })
                voucher.approval_levels = level_data

            # === CAN APPROVE & WAITING FOR (only for PENDING) ===
            can_approve = True
            waiting_for = None

            if (user.groups.filter(name='Admin Staff').exists() or user.is_superuser) and voucher.status == 'PENDING':
                current_level = None
                levels = ApprovalLevel.objects.filter(is_active=True).order_by('order')
                for lvl in levels:
                    users_in_level = UserProfile.objects.filter(
                        designation=lvl.designation,
                        user__groups__name='Admin Staff'
                    ).values_list('user__username', flat=True)
                    if user.username in users_in_level:
                        current_level = lvl
                        break

                if current_level:
                    prev = ApprovalLevel.objects.filter(
                        order__lt=current_level.order, is_active=True
                    ).order_by('-order').first()
                    if prev:
                        prev_users = UserProfile.objects.filter(
                            designation=prev.designation,
                            user__groups__name='Admin Staff'
                        ).values_list('user__username', flat=True)
                        if not all(u in approved_usernames for u in prev_users):
                            can_approve = False
                            waiting_for = prev.designation.name

            voucher.can_approve = can_approve
            voucher.waiting_for_designation = waiting_for

        return context


class VoucherDetailView(LoginRequiredMixin, DetailView):
    model = Voucher
    template_name = 'vouchers/voucher_detail.html'
    context_object_name = 'voucher'

    def get_queryset(self):
        qs = super().get_queryset().select_related('created_by') \
            .prefetch_related('particulars', 'approvals__approver')
        if not self.request.user.is_superuser:
            if self.request.user.groups.filter(name='Accountants').exists():
                qs = qs.filter(created_by=self.request.user)
        return qs.annotate(
            approved_count=Count(Case(When(approvals__status='APPROVED', then=1)), output_field=IntegerField()),
            rejected_count=Count(Case(When(approvals__status='REJECTED', then=1)), output_field=IntegerField())
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        voucher = context['voucher']

        context['is_accountant'] = user.is_authenticated and user.groups.filter(name='Accountants').exists()
        context['is_admin_staff'] = user.is_authenticated and (user.groups.filter(name='Admin Staff').exists() or user.is_superuser)
        context['designations'] = Designation.objects.all()

        try:
            context['user_approval'] = voucher.approvals.get(approver=user)
        except VoucherApproval.DoesNotExist:
            context['user_approval'] = None

        total = len(voucher.required_approvers)
        approved = getattr(voucher, 'approved_count', 0) or 0
        context['approval_percentage'] = (approved / total * 100) if total > 0 else 100

        required = set(voucher.required_approvers)
        approved_usernames = set(
            voucher.approvals.filter(status='APPROVED')
            .values_list('approver__username', flat=True)
        )
        context['pending_approvers'] = [
            {'name': name, 'has_approved': name in approved_usernames}
            for name in required
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
            context['approval_levels'] = level_data
        else:
            # Show snapshot-based progress
            snapshot = voucher.required_approvers_snapshot or []
            context['approval_levels'] = [
                {
                    'designation': {'name': name},
                    'all_approved': name in approved_usernames,
                    'some_approved': name in approved_usernames,
                    'is_next': False
                }
                for name in snapshot
            ]

        # === CAN APPROVE & WAITING FOR ===
        can_approve = True
        waiting_for_designation = None

        if (user.groups.filter(name='Admin Staff').exists() or user.is_superuser) and voucher.status == 'PENDING':
            current_level = None
            levels = ApprovalLevel.objects.filter(is_active=True).order_by('order')
            for lvl in levels:
                users_in_level = UserProfile.objects.filter(
                    designation=lvl.designation,
                    user__groups__name='Admin Staff'
                ).values_list('user__username', flat=True)
                if user.username in users_in_level:
                    current_level = lvl
                    break

            if current_level:
                prev_level = ApprovalLevel.objects.filter(
                    order__lt=current_level.order, is_active=True
                ).order_by('-order').first()
                if prev_level:
                    prev_users = UserProfile.objects.filter(
                        designation=prev_level.designation,
                        user__groups__name='Admin Staff'
                    ).values_list('user__username', flat=True)
                    if not all(u in approved_usernames for u in prev_users):
                        can_approve = False
                        waiting_for_designation = prev_level.designation.name

        context['can_approve'] = can_approve
        context['waiting_for_designation'] = waiting_for_designation
        context['user_profile'] = user.userprofile if hasattr(user, 'userprofile') else None

        return context


# === API VIEWS ===
class VoucherCreateAPI(AccountantRequiredMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.POST.copy()
        files = request.FILES

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

        if 'attachment' not in files:
            return Response(
                {'attachment': 'Main attachment is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        cheque_number = data.get('cheque_number', '').strip() if data.get('payment_type') == 'CHEQUE' else None

        serializer_data = {
            'voucher_date': data.get('voucher_date'),
            'payment_type': data.get('payment_type'),
            'name_title': data.get('name_title'),
            'pay_to': data.get('pay_to'),
            'cheque_number': cheque_number,
            'attachment': files['attachment'],
            'particulars': particulars
        }

        serializer = VoucherSerializer(data=serializer_data, context={'request': request})
        if serializer.is_valid():
            voucher = serializer.save(created_by=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class VoucherApprovalAPI(AdminStaffRequiredMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        status_choice = request.data.get('status')
        if status_choice not in ['APPROVED', 'REJECTED']:
            return Response({'status': ['Invalid choice.']}, status=status.HTTP_400_BAD_REQUEST)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                with transaction.atomic():
                    voucher = Voucher.objects.select_for_update(nowait=True).get(pk=pk)

                    # Use voucher's required_approvers (snapshot or dynamic)
                    if request.user.username not in voucher.required_approvers:
                        return Response(
                            {'error': 'You are not authorized to approve this voucher.'},
                            status=status.HTTP_403_FORBIDDEN
                        )

                    # === SEQUENTIAL APPROVAL: Only for PENDING ===
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

                    # Save approval
                    approval, created = VoucherApproval.objects.update_or_create(
                        voucher=voucher,
                        approver=request.user,
                        defaults={'status': status_choice}
                    )

                    voucher.refresh_from_db()
                    voucher._update_status_if_all_approved()

                serializer = VoucherSerializer(voucher, context={'request': request})
                response_data = serializer.data
                response_data['approval'] = {
                    'approver': request.user.username,
                    'approved_at': approval.approved_at.strftime('%d %b %H:%M')
                }
                response_data['can_approve'] = True
                return Response(response_data, status=status.HTTP_200_OK)

            except OperationalError as e:
                if 'database is locked' in str(e).lower() and attempt < max_retries - 1:
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                else:
                    return Response(
                        {'error': 'Database is busy. Please try again in a moment.'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE
                    )
            except Voucher.DoesNotExist:
                return Response({'error': 'Voucher not found.'}, status=status.HTTP_404_NOT_FOUND)

        return Response(
            {'error': 'Failed to process approval due to database lock.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )


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
            if user_group == 'Admin Staff':
                designation = Designation.objects.get(id=designation_id)
                UserProfile.objects.create(user=user, designation=designation)

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