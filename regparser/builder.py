import copy
import os
import pickle
import re

from lxml import etree

from regparser import api_writer, content
from regparser.federalregister import fetch_notice_json, fetch_notices
from regparser.history.notices import (
    applicable as applicable_notices, group_by_eff_date)
from regparser.history.delays import modify_effective_dates
from regparser.layer import (
    external_citations, formatting, graphics, key_terms, internal_citations,
    interpretations, meta, paragraph_markers, section_by_section,
    table_of_contents, terms)
from regparser.notice.compiler import compile_regulation
from regparser.tree import struct
# from regparser.tree.build import build_whole_regtree
from regparser.tree.xml_parser import reg_text


class Builder(object):
    """Methods used to build all versions of a single regulation, their
    layers, etc. It is largely glue code"""

    def __init__(self, cfr_title, cfr_part, doc_number, checkpointer=None):
        self.cfr_title = cfr_title
        self.cfr_part = cfr_part
        self.doc_number = doc_number
        self.checkpointer = checkpointer or NullCheckpointer()
        self.writer = api_writer.Client()

        self.eff_notices = self.checkpointer.checkpoint(
            "effective-notices",
            lambda: notices_for_cfr_part(self.cfr_title, self.cfr_part)
        )
        self.notices = []
        for notice_group in self.eff_notices.values():
            self.notices.extend(notice_group)

    def write_notices(self):
        for notice in self.notices:
            #  No need to carry this around
            del notice['meta']
            self.writer.notice(notice['document_number']).write(notice)

    def write_regulation(self, reg_tree):
        self.writer.regulation(self.cfr_part, self.doc_number).write(reg_tree)

    def gen_and_write_layers(self, reg_tree, act_info, cache, notices=None):
        if notices is None:
            notices = applicable_notices(self.notices, self.doc_number)
        for ident, layer_class in (
                ('external-citations',
                    external_citations.ExternalCitationParser),
                ('meta', meta.Meta),
                ('analyses', section_by_section.SectionBySection),
                ('internal-citations',
                    internal_citations.InternalCitationParser),
                ('toc', table_of_contents.TableOfContentsLayer),
                ('interpretations', interpretations.Interpretations),
                ('terms', terms.Terms),
                ('paragraph-markers', paragraph_markers.ParagraphMarkers),
                ('keyterms', key_terms.KeyTerms),
                ('formatting', formatting.Formatting),
                ('graphics', graphics.Graphics)):
            layer = self.checkpointer.checkpoint(
                ident + "-" + self.doc_number,
                lambda: layer_class(
                    reg_tree, self.cfr_title, self.doc_number, notices,
                    act_info).build(cache.cache_for(ident)))
            self.writer.layer(ident, self.cfr_part, self.doc_number).write(
                layer)

    def revision_generator(self, reg_tree):
        relevant_notices = []
        for date in sorted(self.eff_notices.keys()):
            relevant_notices.extend(
                n for n in self.eff_notices[date]
                if 'changes' in n and n['document_number'] != self.doc_number)
        for notice in relevant_notices:
            version = notice['document_number']
            old_tree = reg_tree
            merged_changes = self.merge_changes(version, notice['changes'])
            reg_tree = self.checkpointer.checkpoint(
                "compiled-" + version,
                lambda: compile_regulation(old_tree, merged_changes))
            notices = applicable_notices(self.notices, version)
            yield notice, old_tree, reg_tree, notices

    def merge_changes(self, document_number, changes):
        patches = content.RegPatches().get(document_number)
        if patches:
            changes = copy.copy(changes)
            for key in patches:
                if key in changes:
                    changes[key].extend(patches[key])
                else:
                    changes[key] = patches[key]
        return changes

    @staticmethod
    def reg_tree(reg_str):
        if reg_str[:1] == '<':  # XML
            return reg_text.build_tree(reg_str)
        else:
            raise ValueError("Building from text input is no longer "
                             "supported")
            # return build_whole_regtree(reg_str)

    @staticmethod
    def determine_doc_number(reg_str, title, title_part):
        """Instead of requiring the user provide a doc number, we can find it
        within the xml file"""
        # @todo: remove the double-conversion
        reg_xml = etree.fromstring(reg_str)
        doc_number = _fr_doc_to_doc_number(reg_xml)
        if not doc_number:
            doc_number = _fdsys_to_doc_number(reg_xml, title, title_part)
        return doc_number


class LayerCacheAggregator(object):
    """A lot of the reg tree remains the same between versions; we don't
    want to recompute layers every time. This object keeps track of what
    labels are seen/valid."""
    def __init__(self):
        self._known_labels = set()
        self._caches = {}

    def invalidate(self, labels):
        """Given a list of labels, clear out any known labels that would be
        affected. For subpart changes, we just wipe out the whole cache. If
        removing an interpretation, make the logic easier by removing
        related regtext as well."""
        if any('Subpart' in label for label in labels):
            self._known_labels = set()
        else:
            stripped = []
            for label in labels:
                if struct.Node.INTERP_MARK in label:
                    idx = label.find(struct.Node.INTERP_MARK) - 1
                    stripped.append(label[:idx])
                else:
                    stripped.append(label)
            self._known_labels = set(
                known for known in self._known_labels
                if not any(known.startswith(l) for l in stripped))

    def invalidate_by_notice(self, notice):
        """Using the notice structure, invalidate based on the 'changes'
        field"""
        self.invalidate([key for key in notice.get('changes', {})])
        patches = content.RegPatches().get(notice['document_number'], {})
        self.invalidate(patches.keys())

    def is_known(self, label):
        return label in self._known_labels

    def replace_using(self, tree):
        """Clear out the known labels; replace them using the provided node
        tree."""
        self._known_labels = set()

        def per_node(node):
            self._known_labels.add(node.label_id())
        struct.walk(tree, per_node)

    def cache_for(self, layer_name):
        """Get a LayerCache object for a given layer name. Not all layers
        have caches, as caches are currently only used for layers that
        depend on the node's text"""
        if layer_name in ('external-citations', 'internal-citations',
                          'interpretations', 'paragraph-markers', 'keyterms',
                          'formatting', 'graphics'):
            if layer_name not in self._caches:
                self._caches[layer_name] = LayerCache(self)
            return self._caches[layer_name]
        else:
            return EmptyCache()


def notices_for_cfr_part(title, part):
    """Retrieves all final notices for a title-part pair, orders them, and
    returns them as a dict[effective_date_str] -> list(notices)"""
    notices = fetch_notices(title, part, only_final=True)
    modify_effective_dates(notices)
    return group_by_eff_date(notices)


def _fr_doc_to_doc_number(xml):
    """Pull out a document number from an FR document, i.e. a notice"""
    frdoc_els = xml.xpath('//FRDOC')
    if len(frdoc_els) > 0:
        frdoc_pieces = frdoc_els[0].text.split()
        if len(frdoc_pieces) > 2 and frdoc_pieces[:2] == ['[FR', 'Doc.']:
            return frdoc_pieces[2]


def _fdsys_to_doc_number(xml, title, title_part):
    """Pull out a document number from an FDSYS document, i.e. an annual
    edition of a reg"""
    original_date_els = xml.xpath('//FDSYS/ORIGINALDATE')
    if len(original_date_els) > 0:
        date = original_date_els[0].text
        #   Grab oldest document number from Federal register API
        notices = fetch_notice_json(title, title_part, only_final=True,
                                    max_effective_date=date)
        if notices:
            return notices[0]['document_number']


class LayerCache(object):
    """Keeps a cache of a single layer. Used in combination with a
    LayerCacheAggregator to determine when something needs to be recomputed."""
    def __init__(self, parent):
        self.parent = parent
        self._cache = {}

    def fetch_or_process(self, layer, node):
        """Retrieve the value of a layer if known. Otherwise, compute the
        value and cache the result"""
        label = node.label_id()
        if not self.parent.is_known(label):
            self._cache[label] = layer.process(node)
        return self._cache.get(label)


class EmptyCache(object):
    """Dummy cache used to represent layers that should not be cached. For
    example, the toc layer depends on more than the text of its associated
    node, so it should not be cached."""
    def fetch_or_process(self, layer, node):
        return layer.process(node)


def _serialize_xml_fields(node):
    if node.source_xml is not None:
        node.source_xml = etree.tostring(node.source_xml)


def _deserialize_xml_fields(node):
    if node.source_xml:
        node.source_xml = etree.fromstring(node.source_xml)


class Checkpointer(object):
    """Save checkpoints during the build pipeline. Generally, a caller will
    specify, a unique tag (a string) and a fallback function (for how to
    compute it when there is no checkpoint). Calling checkpoint increment the
    counter field, which is prefixed to the filename to limit the risk of
    re-ordering collisions."""
    def __init__(self, file_path):
        self.counter = 0
        self.file_path = file_path
        self.suffix = ""
        self.ignore_checkpoints = False
        if not os.path.isdir(file_path):
            os.makedirs(file_path)

    def _filename(self, tag):
        """Combine the counter and tag name to create a filename"""
        name = str(self.counter).zfill(6) + ":"
        name += re.sub(r"\s", "", tag.lower())
        name += self.suffix + ".p"
        return os.path.join(self.file_path, name)

    def _serialize(self, tag, obj):
        """Performs class-specific conversions before writing to a file"""
        if isinstance(obj, struct.Node):
            obj = copy.deepcopy(obj)
            struct.walk(obj, _serialize_xml_fields)

        with open(self._filename(tag), 'wb') as to_write:
            pickle.dump(obj, to_write)

    def _deserialize(self, tag):
        """Attempts to read the object from disk. Performs class-specific
        conversions when deserializing"""
        name = self._filename(tag)
        if os.path.exists(name):
            with open(name, 'rb') as to_read:
                try:
                    obj = pickle.load(to_read)
                except Exception:   # something bad happened during unpickling
                    obj = None

            if isinstance(obj, struct.Node):
                struct.walk(obj, _deserialize_xml_fields)
            return obj

    def _reset(self):
        """Used for testing"""
        self.counter = 0
        self.ignore_checkpoints = False

    def checkpoint(self, tag, fn, force=False):
        """Primary interface for storing an object"""
        self.counter += 1
        existing = self._deserialize(tag)
        if not force and existing is not None and not self.ignore_checkpoints:
            return existing
        else:
            result = fn()
            self._serialize(tag, result)
            self.ignore_checkpoints = True
            return result


class NullCheckpointer(object):
    def checkpoint(self, tag, fn, force=False):
        return fn()
