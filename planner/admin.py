from django.contrib import admin
from .models import Plan, ItineraryItem, Feedback

admin.site.register(Plan)
admin.site.register(ItineraryItem)
admin.site.register(Feedback)