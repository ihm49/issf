import datetime
import json
import os.path
from typing import Generator, Iterable, List

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.management import call_command
from django.contrib.gis.db.models import Model
from django.urls import reverse
from django.http import HttpResponse, HttpResponseRedirect, Http404, HttpRequest, HttpResponseForbidden
from django.shortcuts import render
from django.template import Context
from djgeojson.views import GeoJSONLayerView
from django.views.decorators.gzip import gzip_page

from issf_base.models import *
from issf_admin.views import get_redirectname
from issf_admin.models import UserProfile

import bleach

from .forms import TipForm, FAQForm, WhosWhoForm, GeoJSONUploadForm
from .forms import SelectedAttributesFormSet, SelectedThemesIssuesFormSet, SearchForm

from django.middleware.gzip import GZipMiddleware

gzip_middleware = GZipMiddleware()


@gzip_page
def index(request: HttpRequest) -> HttpResponse:
    """
    View for rendering the index page of the site.

    :param request: The incoming HTTP request.
    :return: The HTTP response to return to the user.
    """
    # get data for dashboard panels
    recent_contributions = RecentContributions.objects.all()
    for recent_contribution in recent_contributions:
        lines = recent_contribution.core_record_summary.split('\\n')
        summary = ''
        for line in lines:
            if 'Timeframe: ' in line:
                line = line.replace('-0', '')
            if recent_contribution.core_record_type == "State-of-the-Art in SSF Research":
                # Fix for out-of-order authors in summary
                if line.startswith("<strong>Authors:"):
                    line = "<strong>Authors:</strong> "
                    authors = [author.author_name for author in KnowledgeAuthorSimple.objects.filter(knowledge_core=recent_contribution.issf_core_id)]
                    line += ", ".join(authors)
            summary += line + '<br/>'
        recent_contribution.core_record_summary = summary
    contributions_by_record_type = ContributionsByRecordType.objects.all()
    # countries with more than one record
    contributions_by_country = ContributionsByCountry.objects.filter(contribution_count__gt=1)
    # countries with exactly one record
    other_countries = ContributionsByCountry.objects.filter(contribution_count__exact=1)
    contributions_by_geographic_scope = ContributionsByGeographicScope.objects.all()
    attribute_values = AttributeValue.objects.all()
    theme_issue_values = Theme_Issue_Value.objects.all()
    who_feature = WhoFeature.objects.latest('id')
    # return dashboard data and empty search forms
    return render(
        request,
        "frontend/index.html",
        {
            'contributions_by_record_type': contributions_by_record_type,
            'contributions_by_country': contributions_by_country,
            'other_countries': other_countries,
            'contributions_by_geographic_scope': contributions_by_geographic_scope,
            'recent_contributions': recent_contributions,
            'searchForm': SearchForm,
            'selected_themes_issues_formset': SelectedThemesIssuesFormSet,
            'selected_attributes_formset': SelectedAttributesFormSet,
            'attribute_values': attribute_values,
            'theme_issue_values': theme_issue_values,
            'who_feature': who_feature,
            'num_who_records': SSFPerson.objects.all().count(),
            'num_sota_records': SSFKnowledge.objects.all().count(),
            'num_profile_records': SSFProfile.objects.all().count(),
            'num_org_records': SSFOrganization.objects.all().count(),
            'num_cap_records': SSFCapacityNeed.objects.all().count(),
            'num_guide_records': SSFGuidelines.objects.all().count(),
            'num_case_records': SSFCaseStudies.objects.all().count(),
            'num_expe_records': SSFExperiences.objects.all().count(),
        }
    )


def get_map_points(issf_core_ids: Iterable[int]) -> List[ISSFCoreMapPointUnique]:
    """
    Returns map points for all given IDs, if they exist.

    :param issf_core_ids: An iterable containing core ids.
    :return: A list of map points.
    """
    points = []
    for core_id in issf_core_ids:
        points += list(ISSFCoreMapPointUnique.objects.filter(issf_core_id__exact=core_id))
    return points


@gzip_page
def frontend_data(request: HttpRequest) -> HttpResponse:
    """
    View that handles retrieving and searching map points for the front page.

    :param request: The incoming HTTP request.
    :return: The HTTP response to return to the user.
    """

    search_terms = []

    if request.method == 'GET':
        cached_map_data = cache.get('cached_map_data')
        if cached_map_data:
            map_queryset = cached_map_data
        else:
            map_queryset = ISSFCoreMapPointUnique.objects.all()
            cache.set('cached_map_data', map_queryset, 86400)
    else:
        form = SearchForm(request.POST)
        themes_form = SelectedThemesIssuesFormSet(request.POST)
        attributes_form = SelectedAttributesFormSet(request.POST)
        map_queryset = []
        if form.is_valid():
            keywords = form.cleaned_data['keywords']
            fulltext_keywords = form.cleaned_data['fulltext_keywords']
            contributor = form.cleaned_data['contributor']
            countries = form.cleaned_data['countries']
            contribution_begin_date = form.cleaned_data['contribution_begin_date']
            contribution_end_date = form.cleaned_data['contribution_end_date']
        else:
            keywords = None
            fulltext_keywords = None
            contributor = None
            countries = None
            contribution_begin_date = None
            contribution_end_date = None

        themes = []
        if themes_form.is_valid():
            for subform in themes_form.forms:
                try:
                    themes.append(subform.cleaned_data['theme_issue_value'])
                except KeyError:
                    # Occurs when form doesn't have a theme_issue_value value, meaning it isn't valid
                    pass

        attributes = []
        if attributes_form.is_valid():
            for subform in attributes_form:
                try:
                    attributes.append({
                        'attribute': subform.cleaned_data['attribute'],
                        'attribute_value': subform.cleaned_data['attribute_value']
                    })
                except KeyError:
                    # Occurs when form doesn't have an attribute set, meaning it actually isn't valid
                    pass

        models = [
            SSFPerson,
            SSFKnowledge,
            SSFProfile,
            SSFOrganization,
            SSFCapacityNeed,
            SSFGuidelines,
            SSFCaseStudies,
            SSFExperiences
        ]

        if keywords:
            if keywords == "":
                cached_map_data = cache.get('cached_map_data')
                if cached_map_data:
                    map_queryset = cached_map_data
                else:
                    map_queryset = ISSFCoreMapPointUnique.objects.all()
                    cache.set('cached_map_data', map_queryset, 86400)
                map_queryset = list(map_queryset)
            else:
                search_terms.append('Title: {}'.format(str(bleach.clean(keywords))))
                for model in models:
                    # Every object has different variables for "title"
                    if model == SSFPerson:
                        # SSFPerson has no name or title, so we retrieve all matching users from the map points and remove them from the users
                        results = list(model.objects.all())
                        filtered_ids = [i.issf_core_id for i in ISSFCoreMapPointUnique.objects.filter(core_record_summary__icontains=keywords)]
                        for result in results[:]:
                            if result.issf_core_id not in filtered_ids:
                                results.remove(result)
                    elif model == SSFKnowledge:
                        results = model.objects.filter(level1_title__icontains=keywords)
                    elif model == SSFProfile:
                        results = model.objects.filter(ssf_name__icontains=keywords)
                    elif model == SSFOrganization:
                        results = model.objects.filter(organization_name__icontains=keywords)
                    elif model == SSFCapacityNeed:
                        results = model.objects.filter(capacity_need_title__icontains=keywords)
                    elif model == SSFCaseStudies:
                        results = model.objects.filter(name__icontains=keywords)
                    else:
                        results = model.objects.filter(title__icontains=keywords)
                    ids = [result.issf_core_id for result in results]
                    map_queryset += get_map_points(ids)
        else:
            map_queryset = list(ISSFCoreMapPointUnique.objects.all())

        if fulltext_keywords:
            if fulltext_keywords != '':
                search_terms.append('Full text: {}'.format(fulltext_keywords))
                fulltext_matches = set(i.issf_core_id for i in ISSFCoreMapPointUnique.objects.filter(core_record_summary__icontains=fulltext_keywords))
                for item in map_queryset[:]:
                    if item.issf_core_id not in fulltext_matches:
                        map_queryset.remove(item)

        if contributor:
            contributor = int(contributor)
            contributor_instance = tuple(UserProfile.objects.filter(id__exact=contributor))[0]
            contributor_name = contributor_instance.username
            # If the user has defined a name, choose all components of the name that aren't none and add them to the username
            if any([contributor_instance.first_name, contributor_instance.initials, contributor_instance.last_name]):
                components = (i for i in [contributor_instance.first_name, contributor_instance.initials, contributor_instance.last_name] if i is not None)
                contributor_name += ' ({})'.format(' '.join(components))
            search_terms.append('Contributor: {}'.format(contributor_name))
            for item in map_queryset[:]:
                if item.contributor_id != contributor:
                    map_queryset.remove(item)

        if countries:
            unprocessed_countries = countries
            countries = []
            for country in unprocessed_countries:
                try:
                    countries.append(int(country))
                except ValueError:
                    pass

            if len(countries) != 0:
                countries = [int(i) for i in countries]
                country_names = [list(Country.objects.filter(country_id__exact=country))[0].short_name for country in countries]
                # Underlying DB has a column for country id, but model itself doesn't
                # Therefore, we need to fall back to using plain SQL
                country_matches = set(i.issf_core_id for i in ISSFCoreMapPointUnique.objects.raw(
                    'SELECT row_number, issf_core_id, contribution_date, contributor_id, edited_date, editor_id, \
                    core_record_type, core_record_summary, core_record_status geographic_scope_type,\
                    map_point::bytea, lat, lon FROM issf_core_map_point_unique WHERE country_id = ANY(%(countries)s)',
                    {'countries': countries}
                ))
                if len(countries) <= 5:
                    search_terms.append('Countries: {}'.format(', '.join(country_names)))
                else:
                    search_terms.append('Countries: {} (and {} more)'.format(
                        ', '.join(country_names[0:5]),
                        len(countries) - 5
                    ))
                for item in map_queryset[:]:
                    if item.issf_core_id not in country_matches:
                        map_queryset.remove(item)

        if contribution_begin_date:
            search_terms.append('Contribution year begin: {}'.format(contribution_begin_date))
            date = datetime.date(contribution_begin_date, 1, 1)
            matches = set(i.issf_core_id for i in ISSFCoreMapPointUnique.objects.filter(contribution_date__gt=date))
            for item in map_queryset[:]:
                if item.issf_core_id not in matches:
                    map_queryset.remove(item)

        if contribution_end_date:
            search_terms.append('Contribution year end: {}'.format(contribution_end_date))
            date = datetime.date(contribution_end_date, 12, 31)
            matches = set(i.issf_core_id for i in ISSFCoreMapPointUnique.objects.filter(contribution_date__lt=date))
            for item in map_queryset[:]:
                if item.issf_core_id not in matches:
                    map_queryset.remove(item)

        if len(themes) > 0:
            theme_ids = [theme.theme_issue_value_id for theme in themes]
            search_terms.append('Themes / Issues: {}'.format(', '.join((theme.theme_issue_label for theme in themes))))
            matches = set(i.issf_core_id for i in SelectedThemeIssue.objects.filter(theme_issue_value__in=theme_ids))
            for item in map_queryset[:]:
                if item.issf_core_id not in matches:
                    map_queryset.remove(item)

        if len(attributes) > 0:
            attribute_value_ids = []
            for attribute in attributes:
                search_terms.append(
                    '{} {}'.format(
                        attribute['attribute'].attribute_label,
                        attribute['attribute_value']
                    )
                )
                attribute_value_ids.append(attribute['attribute_value'].attribute_value_id)
            matches = set(i.issf_core_id for i in SelectedAttribute.objects.filter(attribute_value__in=attribute_value_ids))
            for item in map_queryset[:]:
                if item.issf_core_id not in matches:
                    map_queryset.remove(item)

    map_results = []

    for row in map_queryset:
        # This code was copied from the previous function
        temp = []
        temp.append(row.core_record_type)
        temp.append(row.geographic_scope_type)
        lines = row.core_record_summary.split('\\n')
        summary = ''
        for line in lines:
            if 'Timeframe: ' in line:
                line = line.replace('-0', '')
            if row.core_record_type == "State-of-the-Art in SSF Research":
                # Fix for out-of-order authors in summary
                if line.startswith("<strong>Authors:"):
                    line = "<strong>Authors:</strong> "
                    authors = [author.author_name for author in KnowledgeAuthorSimple.objects.filter(knowledge_core=row.issf_core_id)]
                    line += ", ".join(authors)
            summary += line + '<br/>'
        temp.append(summary)
        url = reverse(get_redirectname(row.core_record_type), kwargs={'issf_core_id': row.issf_core_id})
        temp.append('<a href="' + url + '">Details</a>')
        temp.append(str(row.lon))
        temp.append(str(row.lat))
        temp.append(row.edited_date.strftime("%Y-%m-%d"))
        # The table originally was sorted by the "relevance", which we no longer have, so we just use a value of 0
        # Removing this value results in errors with the DataTable library
        temp.append("0")
        map_results.append(temp)

    if len(search_terms) == 0:
        search_terms = ["None"]

    joined_terms = ", ".join(search_terms)

    response = json.dumps({
        'success': 'true',
        'msg': 'OK',
        'searchTerms': joined_terms[0].capitalize() + joined_terms[1:] + ' (',
        'mapData': map_results
    })
    return gzip_middleware.process_response(request, HttpResponse(response))


# "Secret" csv exporting URL for GCPC. Only does SSF Profile records and associated characteristics.
# DO NOT MODIFY, THIS URL IS AUTOMATICALLY HIT.
# If any change is made to the database, test this URL to make sure everything still works.
def profile_csv(request: HttpRequest) -> HttpResponse:
    """
    URL that generates and returns a zipfile of csv files for GCPC.
    Now unused.

    :param request: The incoming HTTP request.
    :return: The HTTP response to return to the user.
    """
    profile_records = SSFProfile.objects.all().values(
        'issf_core_id',
        'contributor_id__first_name',
        'contributor_id__last_name',
        'contribution_date',
        'geographic_scope_type',
        'ssf_name',
        'ssf_defined',
        'ssf_definition',
        'data_day',
        'data_month',
        'data_year',
        'data_end_day',
        'data_end_month',
        'data_end_year',
        'comments',
        'sources',
        'percent'
    )
    zipfile = ZipFile('profile_data.zip', 'w')

    main_attrs = MainAttributeView.objects.all().values(
        'issf_core_id',
        'attribute__question_number',
        'attribute__attribute_label',
        'value',
        'attribute__units_label',
        'attribute_value__value_label',
        'other_value',
        'additional',
        'additional_value__value_label'
    )

    write_file_csv('profile.csv', profile_records, zipfile)
    write_file_csv('main_attributes.csv', main_attrs, zipfile)

    geog_scope_local_records = GeographicScopeLocalArea.objects.filter(
        issf_core__core_record_type='SSF Profile').values(
        'geographic_scope_local_area_id',
        'issf_core_id',
        'local_area_name',
        'local_area_alternate_name',
        'country__short_name',
        'local_area_setting',
        'local_area_setting_other',
        'local_area_point'
    )
    geog_scope_regional_records = Geographic_Scope_Region.objects.filter(
        issf_core__core_record_type='SSF Profile').values(
        'geographic_scope_region_id',
        'issf_core_id',
        'region__region_name',
        'region_name_other'
    )
    geog_scope_subnational_records = GeographicScopeSubnation.objects.filter(
        issf_core__core_record_type='SSF Profile').values(
        'geographic_scope_subnation_id',
        'issf_core_id',
        'subnation_name',
        'country__short_name',
        'subnation_type',
        'subnation_type_other',
        'subnation_point'
    )
    geog_scope_national_records = GeographicScopeNation.objects.filter(
        issf_core__core_record_type='SSF Profile').values(
        'geographic_scope_nation_id',
        'issf_core_id',
        'country__short_name'
    )

    write_file_csv('geog_scope_local.csv', geog_scope_local_records, zipfile)
    write_file_csv('geog_scope_regional.csv', geog_scope_regional_records, zipfile)
    write_file_csv('geog_scope_subnational.csv', geog_scope_subnational_records, zipfile)
    write_file_csv('geog_scope_national.csv', geog_scope_national_records, zipfile)

    zipfile.close()
    zipfile = open('profile_data.zip', 'rb')

    response = HttpResponse(zipfile, content_type='application/x-zip-compressed')
    response['Content-Disposition'] = 'attachment; filename="profile_data.zip"'

    return response


class MapLayer(GeoJSONLayerView):
    """
    Map layer containing all map points.
    """
    geometry_field = 'map_point'
    queryset = ISSFCoreMapPointUnique.objects.all()
    properties = ['issf_core_id', 'core_record_type', 'core_record_summary', 'geographic_scope_type']

    def get_context_data(self, **kwargs) -> Context:
        """
        Gets the context data for this map.

        :return: The context data.
        """
        # this case is called from details to show map points for one record
        context = super(MapLayer, self).get_context_data(**kwargs)
        issf_core_id = self.request.GET['issf_core_id']
        self.queryset = ISSFCoreMapPointUnique.objects.filter(issf_core_id=issf_core_id)
        return context


def unique_chain(*iterables: List[Model]) -> Generator[int, None, None]:
    """
    Generator that returns all unique issf_core_ids

    :param iterables: The models to get the unique IDs for.
    :return: A generator of unique IDs.
    """
    known_ids = set()
    for it in iterables:
        for element in it:
            if element['issf_core_id'] not in known_ids:
                known_ids.add(element['issf_core_id'])
                yield element['issf_core_id']


def convert_records(records: Iterable[Model]) -> List[Model]:
    """
    Converts records to a format that is more easily usable for most purposes.

    :param records: The records to convert.
    :return: The converted records.
    """
    records = list(records)
    for i, record in enumerate(records):
        type = record['core_record_type']

        if type == 'SSF Organization':
            record['url'] = 'organization'
            record['title'] = SSFOrganization.objects.get(issf_core_id=record['issf_core_id'])
            record['record_type'] = 'SSF Organization'
        elif type == "Who's Who in SSF":
            record['url'] = 'who'
            record['title'] = SSFPerson.objects.get(issf_core_id=record['issf_core_id'])
            record['record_type'] = "Who's Who in SSF"
        elif type == "State-of-the-Art in SSF Research":
            record['url'] = 'sota'
            record['title'] = SSFKnowledge.objects.get(issf_core_id=record['issf_core_id'])
            record['record_type'] = 'State-of-the-Art in SSF Research'
        elif type == "SSF Profile":
            record['url'] = 'profile'
            record['title'] = SSFProfile.objects.get(issf_core_id=record['issf_core_id'])
            record['record_type'] = 'SSF Profile'
        elif type == "Case Study":
            record['url'] = 'casestudy'
            record['title'] = SSFCaseStudies.objects.get(issf_core_id=record['issf_core_id'])
            record['record_type'] = 'Case Study'
        elif type == "Capacity Development":
            record['url'] = 'capacity'
            record['title'] = SSFCapacityNeed.objects.get(issf_core_id=record['issf_core_id'])
            record['record_type'] = 'Capacity Development'
        elif type == "SSF Experiences":
            record['url'] = 'experiences'
            record['title'] = SSFExperiences.objects.get(issf_core_id=record['issf_core_id'])
            record['record_type'] = 'SSF Experiences'
        elif type == "SSF Guidelines":
            record['url'] = 'guidelines'
            record['title'] = SSFGuidelines.objects.get(issf_core_id=record['issf_core_id'])
            record['record_type'] = 'SSF Guidelines'

        records[i] = record

    return records


def country_records(request: HttpRequest, country_id: int) -> HttpResponse:
    """
    View that handles getting all records for a given country.

    :param request: The incoming HTTP request.
    :param country_id: The ID of the country to get the records for.
    :return: The HTTP response to return to the user.
    """
    nation_records = GeographicScopeNation.objects.filter(country_id=country_id).values()
    region_records = Geographic_Scope_Region.objects.filter(countries__country_id=country_id).values()
    subnation_records = GeographicScopeSubnation.objects.filter(country_id=country_id).values()
    local_records = GeographicScopeLocalArea.objects.filter(country_id=country_id).values()

    record_ids = list(unique_chain(nation_records, region_records, subnation_records, local_records))

    records = ISSF_Core.objects.filter(issf_core_id__in=record_ids).values().order_by('core_record_type')
    records = convert_records(records)

    country = Country.objects.filter(country_id=country_id)
    if country:
        country = country[0]
    else:
        country.short_name = ''

    return render(request, 'frontend/country_records.html', {'records': records, 'country_name': country.short_name})


@login_required()
@gzip_page
def new_tip(request: HttpRequest) -> HttpResponse:
    """
    View to submit a new tip.

    :param request: The incoming HTTP request.
    :return: The HTTP response to return to the user.
    """
    is_staff = False
    if request.user.is_staff:
        is_staff = True
    if is_staff:
        if request.method == 'POST':
            tip_form = TipForm(request.POST)

            if tip_form.is_valid():
                tip_form.save()
                tip_form = TipForm()

            return render(
                request,
                'frontend/new_tip.html',
                {'tip_form': tip_form}
            )

        tip_form = TipForm()
        faq_form = FAQForm()

        return render(
            request,
            'frontend/new_tip.html',
            {
                'tip_form': tip_form,
                'faq_form': faq_form,
                'is_staff': is_staff
            }
        )
    else:
        raise HttpResponseForbidden("Insufficient permission.")


@login_required()
@gzip_page
def new_faq(request: HttpRequest) -> HttpResponse:
    """
    View to submit a new FAQ entry.

    :param request: The incoming HTTP request.
    :return: The HTTP response to return to the user.
    """

    if request.method == 'POST':
        if request.user.is_staff:
            form = FAQForm(request.POST)

            if form.is_valid():
                form.save()

            return HttpResponseRedirect(reverse('new-tip'))
        else:
            raise HttpResponseForbidden("Insufficient permission.")


@login_required()
@gzip_page
def who_feature(request: HttpRequest) -> HttpResponse:
    """
    View for setting the who feature on the front page.

    :param request: The incoming HTTP request.
    :return: The HTTP response to return to the user.
    """
    is_staff = False
    if request.user.is_staff:
        is_staff = True
    if is_staff:
        if request.method == 'POST':
            form = WhosWhoForm(request.POST)

            if form.is_valid():
                form.save()

                return HttpResponseRedirect(reverse('who-feature'))

        form = WhosWhoForm()

        return render(request, 'frontend/who_feature.html', {'form': form})
    else:
        raise Http404("Insufficient permission.")


@login_required()
@gzip_page
def geojson_upload(request: HttpRequest) -> HttpResponse:
    """
    View for uploading a new geojson file.

    :param request: The incoming HTTP request.
    :return: The HTTP response to return to the user.
    """
    is_staff = False
    if request.user.is_staff:
        is_staff = True
    if is_staff:
        if request.method == 'POST':
            form = GeoJSONUploadForm(request.POST, request.FILES)
            if form.is_valid():
                # process uploaded file
                file_ext = form.cleaned_data['file'].name.split('.')[-1]
                if file_ext == 'geojson':
                    BASE = os.path.dirname(os.path.abspath(__file__))
                    with open(os.path.join(BASE, "static/frontend/js/chorodata.json"), 'wb') as destination:
                        destination.write("var choroData = ".encode(encoding='UTF-8'))
                        for chunk in request.FILES['file'].chunks():
                            destination.write(chunk)
                    call_command('collectstatic', verbosity=0, interactive=False)
                    return HttpResponseRedirect(reverse('geojson-upload'))
                else:
                    return render(request, 'frontend/geojson_upload.html', {'form': form, 'form_error': 'File must be of type GeoJSON.'})
        else:
            form = GeoJSONUploadForm()
            return render(request, 'frontend/geojson_upload.html', {'form': form})
