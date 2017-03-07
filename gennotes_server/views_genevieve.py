"""
Genevieve-style notes views.

In theory GenNotes supports various ways of variant annotation -- flexibly
allowing arbitrary tags, like OpenStreetMap.

However, the original use case is Genevieve, and it's possible this is the
only use GenNotes ever sees. So we'll support this natively, enabling editing
within GenNotes directly (i.e. not requiring API use).
"""
import json

from django.contrib.auth.decorators import login_required
from django.core.urlresolvers import reverse
from django.db import transaction
from django.http import Http404
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods

import myvariant
from reversion import revisions as reversion
from reversion.models import Version

from .models import Relation, Variant


def get_chrom_display(chrom):
    if chrom == '23':
        return 'X'
    elif chrom == '24':
        return 'Y'
    elif chrom == '25':
        return 'MT'
    else:
        return chrom


def get_allele_freq(mv_data, var_allele):
    if 'exac' in mv_data:
        exac_data = mv_data['exac']
        exac_url = (
            'http://exac.broadinstitute.org/'
            'variant/{}-{}-{}-{}'.format(
                exac_data['chrom'],
                exac_data['pos'],
                exac_data['ref'],
                var_allele))
        ac = None
        if exac_data['alleles'] == list:
            try:
                idx = exac_data['alleles'].index(var_allele)
                ac = exac_data['ac']['ac'][idx]
            except Exception:
                pass
        else:
            ac = exac_data['ac']['ac']
        if ac:
            an = exac_data['an']['an']
            return ac * 1.0 / an, exac_url
    if 'dbsnp' in mv_data:
        dbsnp_data = mv_data['dbsnp']
        dbsnp_url = 'https://www.ncbi.nlm.nih.gov/SNP/snp_ref.cgi?rs={}'.format(
            dbsnp_data['rsid'])
        if 'alleles' in dbsnp_data:
            for allele in dbsnp_data['alleles']:
                if allele['allele'] == var_allele and 'freq' in allele:
                    return float(allele['freq']), dbsnp_url
    return None, None


def get_var_rel_data(request):
    req_params = ['build', 'chrom', 'pos', 'ref_allele', 'var_allele']
    relation_id = None
    try:
        if request.method == 'GET':
            request_data_dict = request.GET
        elif request.method == 'POST':
            request_data_dict = request.POST
        else:
            raise Http404('Method not recognized.')
        var_data = {k: request_data_dict[k] for k in req_params}
        if 'relation_id' in request_data_dict:
            relation_id = request_data_dict['relation_id']

        if var_data['chrom'] not in Variant.ALLOWED_CHROMS:
            raise Http404('Variant chrom malformed. Chromosomes must be '
                          'numbers: "1", "2", "3"... and "23" for X, "24" '
                          'for Y, and "25" for MT.')
        try:
            pos = int(request_data_dict['pos'])
            assert pos > 0
        except Exception:
            raise Http404('Variant position bad: must be positive integer.')
    except KeyError:
        raise Http404('Not all required parameters present. '
                      'Requests should contain: {}'.format(
                          req_params))

    try:
        variant = Variant.objects.get(
            tags__chrom_b37=request_data_dict['chrom'],
            tags__pos_b37=request_data_dict['pos'],
            tags__ref_allele_b37=request_data_dict['ref_allele'],
            tags__var_allele_b37=request_data_dict['var_allele'],
        )
    except Variant.DoesNotExist:
        variant = None
        relation = None

    if relation_id:
        relation = Relation.objects.get(id=relation_id)
        if not variant or relation.variant.id != variant.id:
            raise Http404("Relation ID not consistent with variant data!")
    else:
        relation = None

    return var_data, variant, relation_id, relation


@transaction.atomic()
@reversion.create_revision()
def commit_variant_edit(var_data, genevieve_effect_data, relation_id,
                        commit_comment, user):
    """
    Custom update method that records revisions and reports new version.
    """
    if commit_comment:
        reversion.set_comment(comment=commit_comment)
    reversion.set_user(user=user)
    variant = Variant.objects.get_or_create(
        tags__chrom_b37=var_data['chrom'],
        tags__pos_b37=var_data['pos'],
        tags__ref_allele_b37=var_data['ref_allele'],
        tags__var_allele_b37=var_data['var_allele'],
    )

    if relation_id:
        relation = Relation.objects.get(id=relation_id)
        relation.tags = genevieve_effect_data
    else:
        relation = Relation(variant=variant,
                            tags=genevieve_effect_data)
    relation.save()
    return relation


def get_mv_data(chrom, pos, ref_allele, var_allele):
    hgvs_format = myvariant.format_hgvs(
        chrom, pos, ref_allele, var_allele)
    mv = myvariant.MyVariantInfo()
    mv_data = mv.getvariant(hgvs_format,
                            fields=['clinvar', 'dbsnp', 'exac'])
    if mv_data and 'clinvar' in mv_data and 'rcv' in mv_data['clinvar']:
        if not type(mv_data['clinvar']['rcv']) == list:
            mv_data['clinvar']['rcv'] = [mv_data['clinvar']['rcv']]
    if mv_data:
        allele_freq, freq_url = get_allele_freq(mv_data, var_allele)
    return hgvs_format, mv_data, allele_freq, freq_url


@login_required
@require_http_methods(["GET", "POST"])
def genevieve_edit(request):
    template_name = 'gennotes_server/genevieve_edit.html'

    var_data, variant, relation_id, relation = get_var_rel_data(request)

    var_data['chrom_display'] = get_chrom_display(var_data['chrom'])
    hgvs_format, mv_data, allele_freq, freq_url = get_mv_data(
        var_data['chrom'], var_data['pos'],
        var_data['ref_allele'], var_data['var_allele'])
    var_data['hgvs_format'] = hgvs_format
    if allele_freq:
        var_data['allele_freq'] = allele_freq
        var_data['freq_url'] = freq_url

    if request.method == 'GET':
        return render(
            request,
            template_name,
            context={
                'build': 'b37',
                'var_data': var_data,
                'mv_data': mv_data,
                'effect_data': relation.tags if relation else None,
                'relation_id': relation_id,
            }
        )
    elif request.method == 'POST':
        genevieve_effect_data = {
            'type': 'genevieve_effect',
            'category': request.POST['genevieve_effect_category'],
            'significance': request.POST['genevieve_effect_significance'],
            'name': request.POST['genevieve_effect_name'],
            'inheritance': request.POST['genevieve_effect_inheritance'],
            'evidence': request.POST['genevieve_effect_evidence'],
            'notes': request.POST['genevieve_effect_notes'],
            'clinvar_rcv_records': request.POST.getlist(
                'genevieve_effect_clinvar_rcv_records'),
        }
        if 'commit_comment' in request.POST:
            commit_comment = request.POST['commit_comment']
        else:
            commit_comment = None
        relation = commit_variant_edit(
            var_data, genevieve_effect_data, relation_id,
            commit_comment, request.user)
        return redirect(reverse('genevieve_relation_display',
                                kwargs={'rel_id': relation.id}))

@require_http_methods(["GET"])
def genevieve_relation_display(request, rel_id):
    try:
        relation = Relation.objects.get(id=rel_id)
        version = None
    except Exception:
        raise Http404("Effect not found.")
    if 'version_id' in request.GET:
        try:
            version = Version.objects.get(id=request.GET['version_id'])
            serialized_data = json.loads(version.serialized_data)
            relation.tags = serialized_data[0]['fields']['tags']
        except Exception:
            raise Http404("Version not found.")
    variant = relation.variant
    chrom_display = get_chrom_display(variant.tags['chrom_b37'])
    hgvs_format, mv_data, allele_freq, freq_url = get_mv_data(
        variant.tags['chrom_b37'], variant.tags['pos_b37'],
        variant.tags['ref_allele_b37'], variant.tags['var_allele_b37'])
    versions = Version.objects.filter(
        content_type__app_label='gennotes_server',
        content_type__model='relation',
        object_id=rel_id).order_by('revision__date_created').reverse()
    contradicted = True if relation.tags['evidence'] == 'contradicted' else False
    reported = True if relation.tags['evidence'] == 'reported' else False
    return render(
        request, 'gennotes_server/genevieve_relation_display.html',
        context={
            'relation': relation,
            'chrom_display': chrom_display,
            'hgvs_format': hgvs_format,
            'allele_freq': allele_freq,
            'freq_url': freq_url,
            'contradicted': contradicted,
            'reported': reported,
            'edit_history': versions,
            'version': version,
        })
