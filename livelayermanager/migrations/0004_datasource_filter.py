# -*- coding: utf-8 -*-
# Generated by Django 1.9.7 on 2016-06-28 06:53
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('livelayermanager', '0003_auto_20160628_1436'),
    ]

    operations = [
        migrations.AddField(
            model_name='datasource',
            name='filter',
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
    ]