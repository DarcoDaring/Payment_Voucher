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
    ApprovalLevel, UserProfile, AccountDetail, CompanyDetail,
    MainAttachment, ChequeAttachment, ParticularAttachment
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
    ordering = ['-created_at']

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
        
        # Safely load company details
        try:
            context['company'] = CompanyDetail.load()
        except Exception as e:
            context['company'] = None
            print(f"Error loading company details: {e}")

        return context
# === FINAL VOUCHER CREATE/EDIT API – FULLY WORKING EDIT MODE ===
class VoucherCreateAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.POST.copy()
        files = request.FILES

        voucher_id = data.get('voucher_id')
        is_edit = bool(voucher_id)

        try:
            with transaction.atomic():
                # -------------------------------------------------
                # 1. Get or create the voucher
                # -------------------------------------------------
                if is_edit:
                    voucher = Voucher.objects.select_for_update().get(
                        id=voucher_id,
                        created_by=request.user,
                        status='PENDING',
                        approvals__isnull=True  # only editable if no one approved yet
                    )
                else:
                    voucher = Voucher(created_by=request.user)

                # Basic fields
                voucher.voucher_date = data['voucher_date']
                voucher.payment_type = data['payment_type']
                voucher.name_title = data['name_title']
                voucher.pay_to = data['pay_to']

                # Cheque fields
                if data['payment_type'] == 'CHEQUE':
                    voucher.cheque_number = data.get('cheque_number', '').strip()
                    voucher.cheque_date = data.get('cheque_date') or None
                    voucher.account_details_id = data.get('account_details') or None

                    if not voucher.cheque_number:
                        return Response({'error': 'Cheque number is required.'}, status=400)
                    if not voucher.cheque_date:
                        return Response({'error': 'Cheque date is required.'}, status=400)
                    if not voucher.account_details:
                        return Response({'error': 'Account Details is required.'}, status=400)
                else:
                    voucher.cheque_number = voucher.cheque_date = voucher.account_details = None

                voucher.save()  # generates voucher_number on create

                # -------------------------------------------------
                # 2. Main attachments (multiple, optional)
                # -------------------------------------------------
                main_files = files.getlist('main_attachments')

                if is_edit:
                    # EDIT MODE
                    if main_files:
                        # User uploaded new main files → replace all old ones
                        MainAttachment.objects.filter(voucher=voucher).delete()
                        for f in main_files:
                            MainAttachment.objects.create(voucher=voucher, file=f)
                    # If no new files uploaded → keep existing old ones (do nothing)
                else:
                    # CREATE MODE
                    if main_files:
                        # User uploaded main files → create them
                        for f in main_files:
                            MainAttachment.objects.create(voucher=voucher, file=f)
                    # If no files uploaded → that's OK, main attachments are optional
                # -------------------------------------------------
                # 3. Cheque attachments – FINAL FIXED VERSION (Create + Edit)
                # -------------------------------------------------
                if data['payment_type'] == 'CHEQUE':
                    cheque_files = files.getlist('cheque_attachments')

                    if is_edit:
                        # EDIT MODE
                        if cheque_files:
                            # User uploaded new cheque images → replace all old ones
                            ChequeAttachment.objects.filter(voucher=voucher).delete()
                            for f in cheque_files:
                                ChequeAttachment.objects.create(voucher=voucher, file=f)
                        # If no new files uploaded → keep old ones (don't delete)
                    else:
                        # CREATE MODE
                        if not cheque_files:
                            return Response({'error': 'At least one cheque attachment is required for Cheque payments.'}, status=400)
                        
                        for f in cheque_files:
                            ChequeAttachment.objects.create(voucher=voucher, file=f)

                    # Final validation: ensure at least one cheque attachment exists
                    if not ChequeAttachment.objects.filter(voucher=voucher).exists():
                        return Response({'error': 'At least one cheque attachment is required for Cheque payments.'}, status=400)

                else:
                    # Not cheque → remove any old cheque attachments
                    ChequeAttachment.objects.filter(voucher=voucher).delete()
                # -------------------------------------------------
                # 4. Particulars + attachments (UPDATED FOR PROPER EDIT SUPPORT)
                # -------------------------------------------------
                # REMOVED: Unconditional deletion of old particulars in edit mode
                # Now: Update/create/delete based on sent data (assumes frontend sends in existing order)

                i = 0
                if is_edit:
                    # Get existing particulars in consistent order (adjust order_by if needed, e.g., 'created_at')
                    existing_particulars = list(voucher.particulars.order_by('id'))
                else:
                    existing_particulars = []

                while f"particulars[{i}][description]" in data:
                    desc = data[f"particulars[{i}][description]"].strip()
                    amt_str = data[f"particulars[{i}][amount]"].strip()

                    if not desc or not amt_str:
                        i += 1
                        continue

                    try:
                        amount = Decimal(amt_str)
                        if amount <= 0:
                            return Response({f'particular_{i}': 'Amount must be > 0'}, status=400)
                    except InvalidOperation:
                        return Response({f'particular_{i}': 'Invalid amount'}, status=400)

                    new_files = files.getlist(f'particular_attachment_{i}')

                    if i < len(existing_particulars):
                        # UPDATE existing particular
                        particular = existing_particulars[i]
                        particular.description = desc
                        particular.amount = amount
                        particular.save()

                        if new_files:
                            # REPLACE attachments: delete old physical files and DB records
                            for attachment in particular.attachments.all():
                                if attachment.file:
                                    attachment.file.delete(save=False)
                            particular.attachments.all().delete()
                            # Add new ones
                            for f in new_files:
                                ParticularAttachment.objects.create(particular=particular, file=f)
                        # ELSE: Keep existing attachments (no error!)

                    else:
                        # CREATE new particular
                        particular = Particular(voucher=voucher, description=desc, amount=amount)
                        particular.save()

                        if not new_files:
                            return Response(
                                {f'particular_{i}': 'At least one attachment required for new particular'},
                                status=400
                            )
                        # Add attachments
                        for f in new_files:
                            ParticularAttachment.objects.create(particular=particular, file=f)

                    i += 1

                # DELETE extra existing particulars (if frontend sent fewer)
                for j in range(i, len(existing_particulars)):
                    extra = existing_particulars[j]
                    # Delete physical files first
                    for attachment in extra.attachments.all():
                        if attachment.file:
                            attachment.file.delete(save=False)
                    # Then delete DB records
                    extra.attachments.all().delete()
                    extra.delete()

                # Final validation: at least one particular
                if voucher.particulars.count() == 0:
                    return Response({'error': 'At least one particular is required.'}, status=400)
                
                # Optional: Ensure every particular has at least one attachment (catches edge cases like replacing with empty)
                for particular in voucher.particulars.all():
                    if not particular.attachments.exists():
                        return Response({
                            'error': f'Particular "{particular.description[:50]}..." requires at least one attachment.'
                        }, status=400)

                action = "updated" if is_edit else "created"
                return Response({
                    'success': True,
                    'message': f'Voucher {voucher.voucher_number} {action} successfully!',
                    'voucher': {
                        'id': voucher.id,
                        'voucher_number': voucher.voucher_number
                    }
                }, status=200 if is_edit else 201)

        except Voucher.DoesNotExist:
            return Response({'error': 'Voucher not found or cannot be edited.'}, status=404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return Response({'error': str(e)}, status=500)
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
            # 1. Save new workflow
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

            # 2. RECALCULATE ALL EXISTING VOUCHERS WITH LOCKING (PREVENTS RACE CONDITIONS)
            current_required_designation_ids = list(
                ApprovalLevel.objects
                .filter(is_active=True)
                .order_by('order')
                .values_list('designation_id', flat=True)
            )

            vouchers_to_check = Voucher.objects.filter(status__in=['PENDING', 'APPROVED']).select_for_update()

            for voucher in vouchers_to_check:
                approved_count = VoucherApproval.objects.filter(
                    voucher=voucher,
                    status='APPROVED',
                    approver__userprofile__designation_id__in=current_required_designation_ids
                ).values('approver__userprofile__designation_id').distinct().count()

                required_count = len(current_required_designation_ids)

                if required_count > 0 and approved_count >= required_count:
                    voucher.status = 'APPROVED'
                    voucher.save(update_fields=['status'])
                elif required_count == 0:
                    voucher.status = 'APPROVED'
                    voucher.save(update_fields=['status'])

        return Response({
            'message': 'Approval workflow updated and all vouchers recalculated!'
        }, status=status.HTTP_200_OK)
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
        signature = request.FILES.get('signature')

        # CASE 1: Superuser editing ANY user (from User Control Panel)
        if request.user.is_superuser and request.data.get('user_id'):
            user_id = request.data.get('user_id')
            username = request.data.get('username', '').strip()
            group_name = request.data.get('user_group')
            designation_id = request.data.get('designation')
            is_active = request.data.get('is_active') in [True, 'true', 'True']

            if not user_id or not group_name or not username:
                return Response({'error': 'Missing required fields'}, status=400)

            try:
                user = User.objects.get(id=user_id)
            except User.DoesNotExist:
                return Response({'error': 'User not found'}, status=404)

            if username != user.username and User.objects.filter(username=username).exists():
                return Response({'error': 'Username already taken'}, status=400)

            user.username = username
            user.is_active = is_active
            user.save()

            user.groups.clear()
            try:
                group = Group.objects.get(name=group_name)
                user.groups.add(group)
            except Group.DoesNotExist:
                return Response({'error': 'Invalid group'}, status=400)

            profile, _ = UserProfile.objects.get_or_create(user=user)
            if group_name == 'Admin Staff':
                if not designation_id:
                    return Response({'error': 'Designation required for Admin Staff'}, status=400)
                try:
                    designation = Designation.objects.get(id=designation_id)
                    profile.designation = designation
                except Designation.DoesNotExist:
                    return Response({'error': 'Invalid designation'}, status=400)
            else:
                profile.designation = None

            if signature:
                profile.signature = signature
            profile.save()

            return Response({'message': 'User updated successfully'}, status=200)

        # CASE 2: Any logged-in user updating ONLY their OWN signature
        else:
            if not signature:
                return Response({'error': 'Please select a signature image'}, status=400)

            profile, created = UserProfile.objects.get_or_create(user=request.user)
            profile.signature = signature
            profile.save()

            return Response({
                'message': 'Signature updated successfully!',
                'signature_url': profile.signature.url
            }, status=200)
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