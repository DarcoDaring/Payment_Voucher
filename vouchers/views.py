# vouchers/views.py
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
from .models import Voucher, Particular, VoucherApproval, Designation, ActiveApprovalDesignation, UserProfile
from .serializers import VoucherSerializer, VoucherApprovalSerializer
from django.contrib.auth.models import User, Group
from django.contrib.auth.hashers import make_password
from django.db import transaction
from decimal import Decimal, InvalidOperation


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
        context['designations'] = Designation.objects.all()
        return context


class VoucherListView(LoginRequiredMixin, ListView):
    model = Voucher
    template_name = 'vouchers/voucher_list.html'
    context_object_name = 'vouchers'
    paginate_by = 10

    def get_queryset(self):
        qs = super().get_queryset().select_related('created_by')
        qs = qs.prefetch_related(
            'particulars',
            'approvals',
            'approvals__approver',
        )
        if self.request.user.groups.filter(name='Accountants').exists():
            qs = qs.filter(created_by=self.request.user)

        return qs.annotate(
            approved_count=Count(
                Case(When(approvals__status='APPROVED', then=1)),
                output_field=IntegerField()
            ),
            rejected_count=Count(
                Case(When(approvals__status='REJECTED', then=1)),
                output_field=IntegerField()
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['designations'] = Designation.objects.all()
        for voucher in context['vouchers']:
            try:
                voucher.user_approval = voucher.approvals.get(approver=self.request.user)
            except VoucherApproval.DoesNotExist:
                voucher.user_approval = None

            required = set(voucher.required_approvers)
            approved = set(voucher.approvals.filter(status='APPROVED').values_list('approver__username', flat=True))
            voucher.pending_approvers = [
                {'name': name, 'has_approved': name in approved}
                for name in required
            ]
        return context


class VoucherDetailView(LoginRequiredMixin, DetailView):
    model = Voucher
    template_name = 'vouchers/voucher_detail.html'
    context_object_name = 'voucher'

    def get_queryset(self):
        return super().get_queryset().select_related('created_by').prefetch_related(
            'particulars', 'approvals__approver'
        ).annotate(
            approved_count=Count(
                Case(When(approvals__status='APPROVED', then=1)),
                output_field=IntegerField()
            ),
            rejected_count=Count(
                Case(When(approvals__status='REJECTED', then=1)),
                output_field=IntegerField()
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['designations'] = Designation.objects.all()
        voucher = context['voucher']
        try:
            context['user_approval'] = voucher.approvals.get(approver=self.request.user)
        except VoucherApproval.DoesNotExist:
            context['user_approval'] = None

        total = len(voucher.required_approvers)
        approved = getattr(voucher, 'approved_count', 0) or 0
        context['approval_percentage'] = (approved / total * 100) if total > 0 else 0

        required = set(voucher.required_approvers)
        approved_usernames = set(voucher.approvals.filter(status='APPROVED').values_list('approver__username', flat=True))
        context['pending_approvers'] = [
            {'name': name, 'has_approved': name in approved_usernames}
            for name in required
        ]
        return context


# === API VIEWS ===
class VoucherCreateAPI(AccountantRequiredMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.POST.copy()
        files = request.FILES

        # ---------- Re-build particulars ----------
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

        # ---------- Main attachment ----------
        if 'attachment' not in files:
            return Response(
                {'attachment': 'Main attachment is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ---------- Cheque number ----------
        cheque_number = data.get('cheque_number', '').strip() if data.get('payment_type') == 'CHEQUE' else None

        # ---------- Serializer data ----------
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
        voucher = get_object_or_404(Voucher, pk=pk)
        status_choice = request.data.get('status')

        if status_choice not in ['APPROVED', 'REJECTED']:
            return Response({'status': ['Invalid choice.']}, status=400)

        if request.user.username not in voucher.required_approvers:
            return Response({'error': 'You are not authorized to approve this voucher.'}, status=403)

        with transaction.atomic():
            approval, created = VoucherApproval.objects.update_or_create(
                voucher=voucher,
                approver=request.user,
                defaults={'status': status_choice}
            )
            voucher._update_status_if_all_approved()

        serializer = VoucherSerializer(voucher, context={'request': request})
        response_data = serializer.data
        response_data['approval'] = {
            'approver': request.user.username,
            'approved_at': approval.approved_at.strftime('%d %b %H:%M')
        }
        return Response(response_data)


# === REMOVED: CreateUserView (replaced by modal + UserCreateAPI) ===


class DesignationCreateAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser Computation'}, status=403)

        name = request.data.get('name', '').strip()
        if not name:
            return Response({'error': 'Name is required'}, status=400)

        if Designation.objects.filter(name=name).exists():
            return Response({'error': 'Designation already exists'}, status=400)

        designation = Designation.objects.create(name=name, created_by=request.user)
        return Response({
            'message': f"Designation '{designation.name}' created.",
            'id': designation.id
        }, status=201)


class ApprovalControlAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=403)

        active_designations = ActiveApprovalDesignation.objects.filter(is_active=True).values_list('designation__id', flat=True)
        all_designations = Designation.objects.values('id', 'name')
        
        return Response({
            'active_designations': list(active_designations),
            'all_designations': list(all_designations)
        })

    def post(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=403)

        active_ids = request.data.get('active_designations', [])
        if not isinstance(active_ids, list):
            return Response({'error': 'active_designations must be a list of IDs'}, status=400)

        for designation in Designation.objects.all():
            is_active = str(designation.id) in [str(x) for x in active_ids]
            ActiveApprovalDesignation.objects.update_or_create(
                designation=designation,
                defaults={'is_active': is_active, 'updated_by': request.user}
            )

        return Response({
            'message': f'Approval workflow updated. {len(active_ids)} active designations.',
            'active_count': len(active_ids)
        })


# === NEW: USER CREATE VIA MODAL (AJAX) ===
class UserCreateAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_superuser:
            return Response({'error': 'Superuser only'}, status=403)

        username = request.data.get('username', '').strip()
        password = request.data.get('password', '')
        user_group = request.data.get('user_group', '')
        designation_id = request.data.get('designation')

        # Validation
        if not username or not password or not user_group:
            return Response({'error': 'Username, password, and group are required'}, status=400)
        if User.objects.filter(username=username).exists():
            return Response({'error': 'Username already exists'}, status=400)
        if user_group not in ['Accountants', 'Admin Staff']:
            return Response({'error': 'Invalid group'}, status=400)
        if user_group == 'Admin Staff' and not designation_id:
            return Response({'error': 'Designation is required for Admin Staff'}, status=400)
        if len(password) < 8:
            return Response({'error': 'Password must be at least 8 characters'}, status=400)

        try:
            # Create user
            user = User.objects.create(
                username=username,
                password=make_password(password)
            )

            # Assign group
            group = Group.objects.get(name=user_group)
            user.groups.add(group)

            # Assign designation if Admin Staff
            if user_group == 'Admin Staff':
                designation = Designation.objects.get(id=designation_id)
                UserProfile.objects.create(user=user, designation=designation)

            return Response({
                'message': f'User "{username}" created successfully.',
                'id': user.id
            }, status=201)

        except Group.DoesNotExist:
            return Response({'error': 'Group does not exist'}, status=400)
        except Designation.DoesNotExist:
            return Response({'error': 'Invalid designation'}, status=400)
        except Exception as e:
            return Response({'error': str(e)}, status=500)