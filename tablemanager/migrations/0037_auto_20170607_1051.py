# -*- coding: utf-8 -*-
# Generated by Django 1.9.7 on 2017-06-07 02:51
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tablemanager', '0036_auto_20161202_1011'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='input',
            name='spatial_type',
        ),
        migrations.RemoveField(
            model_name='publish',
            name='spatial_type',
        ),
        migrations.AddField(
            model_name='input',
            name='spatial_info',
            field=models.TextField(blank=True, editable=False, max_length=512, null=True),
        ),
        migrations.AddField(
            model_name='publish',
            name='spatial_info',
            field=models.TextField(blank=True, editable=False, max_length=512, null=True),
        ),
    ]
