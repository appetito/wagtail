from __future__ import unicode_literals

import copy

from six import text_type

from modelcluster.forms import ClusterForm, ClusterFormMetaclass


from django.db import models
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from django import forms
from django.forms.models import fields_for_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ImproperlyConfigured
from django.utils.translation import ugettext_lazy

from taggit.managers import TaggableManager

from wagtail.wagtailadmin import widgets
from wagtail.wagtailcore.models import Page
from wagtail.wagtailcore.utils import camelcase_to_underscore, resolve_model_string


FORM_FIELD_OVERRIDES = {
    models.DateField: {'widget': widgets.AdminDateInput},
    models.TimeField: {'widget': widgets.AdminTimeInput},
    models.DateTimeField: {'widget': widgets.AdminDateTimeInput},
    TaggableManager: {'widget': widgets.AdminTagWidget}
}


# Callback to allow us to override the default form fields provided for each model field.
def formfield_for_dbfield(db_field, **kwargs):
    # snarfed from django/contrib/admin/options.py

    # If we've got overrides for the formfield defined, use 'em. **kwargs
    # passed to formfield_for_dbfield override the defaults.
    for klass in db_field.__class__.mro():
        if klass in FORM_FIELD_OVERRIDES:
            kwargs = dict(copy.deepcopy(FORM_FIELD_OVERRIDES[klass]), **kwargs)
            return db_field.formfield(**kwargs)

    # For any other type of field, just call its formfield() method.
    return db_field.formfield(**kwargs)


def widget_with_script(widget, script):
    return mark_safe('{0}<script>{1}</script>'.format(widget, script))


class WagtailAdminModelFormMetaclass(ClusterFormMetaclass):
    # Override the behaviour of the regular ModelForm metaclass -
    # which handles the translation of model fields to form fields -
    # to use our own formfield_for_dbfield function to do that translation.
    # This is done by sneaking a formfield_callback property into the class
    # being defined (unless the class already provides a formfield_callback
    # of its own).

    # while we're at it, we'll also set extra_form_count to 0, as we're creating
    # extra forms in JS
    extra_form_count = 0

    def __new__(cls, name, bases, attrs):
        if 'formfield_callback' not in attrs or attrs['formfield_callback'] is None:
            attrs['formfield_callback'] = formfield_for_dbfield

        new_class = super(WagtailAdminModelFormMetaclass, cls).__new__(cls, name, bases, attrs)
        return new_class

WagtailAdminModelForm = WagtailAdminModelFormMetaclass(str('WagtailAdminModelForm'), (ClusterForm,), {})

# Now, any model forms built off WagtailAdminModelForm instead of ModelForm should pick up
# the nice form fields defined in FORM_FIELD_OVERRIDES.


def get_form_for_model(
    model,
    fields=None, exclude=None, formsets=None, exclude_formsets=None, widgets=None
):

    # django's modelform_factory with a bit of custom behaviour
    # (dealing with Treebeard's tree-related fields that really should have
    # been editable=False)
    attrs = {'model': model}

    if fields is not None:
        attrs['fields'] = fields

    if exclude is not None:
        attrs['exclude'] = exclude
    if issubclass(model, Page):
        attrs['exclude'] = attrs.get('exclude', []) + ['content_type', 'path', 'depth', 'numchild']

    if widgets is not None:
        attrs['widgets'] = widgets

    if formsets is not None:
        attrs['formsets'] = formsets

    if exclude_formsets is not None:
        attrs['exclude_formsets'] = exclude_formsets

    # Give this new form class a reasonable name.
    class_name = model.__name__ + str('Form')
    form_class_attrs = {
        'Meta': type(str('Meta'), (object,), attrs)
    }

    return WagtailAdminModelFormMetaclass(class_name, (WagtailAdminModelForm,), form_class_attrs)


def extract_panel_definitions_from_model_class(model, exclude=None):
    if hasattr(model, 'panels'):
        return model.panels

    panels = []

    _exclude = []
    if exclude:
        _exclude.extend(exclude)
    if issubclass(model, Page):
        _exclude = ['content_type', 'path', 'depth', 'numchild']

    fields = fields_for_model(model, exclude=_exclude, formfield_callback=formfield_for_dbfield)

    for field_name, field in fields.items():
        try:
            panel_class = field.widget.get_panel()
        except AttributeError:
            panel_class = FieldPanel

        panel = panel_class(field_name)
        panels.append(panel)

    return panels


class EditHandler(object):
    """
    Abstract class providing sensible default behaviours for objects implementing
    the EditHandler API
    """

    # return list of widget overrides that this EditHandler wants to be in place
    # on the form it receives
    @classmethod
    def widget_overrides(cls):
        return {}

    # return list of fields that this EditHandler expects to find on the form
    @classmethod
    def required_fields(cls):
        return []

    # return a dict of formsets that this EditHandler requires to be present
    # as children of the ClusterForm; the dict is a mapping from relation name
    # to parameters to be passed as part of get_form_for_model's 'formsets' kwarg
    @classmethod
    def required_formsets(cls):
        return {}

    # the top-level edit handler is responsible for providing a form class that can produce forms
    # acceptable to the edit handler
    _form_class = None

    @classmethod
    def get_form_class(cls, model):
        if cls._form_class is None:
            cls._form_class = get_form_for_model(
                model,
                fields=cls.required_fields(),
                formsets=cls.required_formsets(), widgets=cls.widget_overrides())
        return cls._form_class

    def __init__(self, instance=None, form=None):
        if not instance:
            raise ValueError("EditHandler did not receive an instance object")
        self.instance = instance

        if not form:
            raise ValueError("EditHandler did not receive a form object")
        self.form = form

    # Heading / help text to display to the user
    heading = ""
    help_text = ""

    def classes(self):
        """
        Additional CSS classnames to add to whatever kind of object this is at output.
        Subclasses of EditHandler should override this, invoking super(B, self).classes() to
        append more classes specific to the situation.
        """

        classes = []

        try:
            classes.append(self.classname)
        except AttributeError:
            pass

        return classes

    def field_type(self):
        """
        The kind of field it is e.g boolean_field. Useful for better semantic markup of field display based on type
        """
        return ""

    def render_as_object(self):
        """
        Render this object as it should appear within an ObjectList. Should not
        include the <h2> heading or help text - ObjectList will supply those
        """
        # by default, assume that the subclass provides a catch-all render() method
        return self.render()

    def render_as_field(self):
        """
        Render this object as it should appear within a <ul class="fields"> list item
        """
        # by default, assume that the subclass provides a catch-all render() method
        return self.render()

    def render_missing_fields(self):
        """
        Helper function: render all of the fields that are defined on the form but not "claimed" by
        any panels via required_fields. These fields are most likely to be hidden fields introduced
        by the forms framework itself, such as ORDER / DELETE fields on formset members.

        (If they aren't actually hidden fields, then they will appear as ugly unstyled / label-less fields
        outside of the panel furniture. But there's not much we can do about that.)
        """
        rendered_fields = self.required_fields()
        missing_fields_html = [
            text_type(self.form[field_name])
            for field_name in self.form.fields
            if field_name not in rendered_fields
        ]

        return mark_safe(''.join(missing_fields_html))

    def render_form_content(self):
        """
        Render this as an 'object', ensuring that all fields necessary for a valid form
        submission are included
        """
        return mark_safe(self.render_as_object() + self.render_missing_fields())


class BaseCompositeEditHandler(EditHandler):
    """
    Abstract class for EditHandlers that manage a set of sub-EditHandlers.
    Concrete subclasses must attach a 'children' property
    """
    _widget_overrides = None

    @classmethod
    def widget_overrides(cls):
        if cls._widget_overrides is None:
            # build a collated version of all its children's widget lists
            widgets = {}
            for handler_class in cls.children:
                widgets.update(handler_class.widget_overrides())
            cls._widget_overrides = widgets

        return cls._widget_overrides

    _required_fields = None

    @classmethod
    def required_fields(cls):
        if cls._required_fields is None:
            fields = []
            for handler_class in cls.children:
                fields.extend(handler_class.required_fields())
            cls._required_fields = fields

        return cls._required_fields

    _required_formsets = None

    @classmethod
    def required_formsets(cls):
        if cls._required_formsets is None:
            formsets = {}
            for handler_class in cls.children:
                formsets.update(handler_class.required_formsets())
            cls._required_formsets = formsets

        return cls._required_formsets

    def __init__(self, instance=None, form=None):
        super(BaseCompositeEditHandler, self).__init__(instance=instance, form=form)

        self.children = [
            handler_class(instance=self.instance, form=self.form)
            for handler_class in self.__class__.children
        ]

    def render(self):
        return mark_safe(render_to_string(self.template, {
            'self': self
        }))


class BaseTabbedInterface(BaseCompositeEditHandler):
    template = "wagtailadmin/edit_handlers/tabbed_interface.html"


def TabbedInterface(children):
    return type(str('_TabbedInterface'), (BaseTabbedInterface,), {'children': children})


class BaseObjectList(BaseCompositeEditHandler):
    template = "wagtailadmin/edit_handlers/object_list.html"


def ObjectList(children, heading="", classname=""):
    return type(str('_ObjectList'), (BaseObjectList,), {
        'children': children,
        'heading': heading,
        'classname': classname
    })


class BaseFieldRowPanel(BaseCompositeEditHandler):
    template = "wagtailadmin/edit_handlers/field_row_panel.html"


def FieldRowPanel(children, classname=""):
    return type(str('_FieldRowPanel'), (BaseFieldRowPanel,), {
        'children': children,
        'classname': classname,
    })


class BaseMultiFieldPanel(BaseCompositeEditHandler):
    template = "wagtailadmin/edit_handlers/multi_field_panel.html"

    def classes(self):
        classes = super(BaseMultiFieldPanel, self).classes()
        classes.append("multi-field")

        return classes


def MultiFieldPanel(children, heading="", classname=""):
    return type(str('_MultiFieldPanel'), (BaseMultiFieldPanel,), {
        'children': children,
        'heading': heading,
        'classname': classname,
    })


class BaseFieldPanel(EditHandler):

    @classmethod
    def widget_overrides(cls):
        """check if a specific widget has been defined for this field"""
        if hasattr(cls, 'widget'):
            return {cls.field_name: cls.widget}
        else:
            return {}

    def __init__(self, instance=None, form=None):
        super(BaseFieldPanel, self).__init__(instance=instance, form=form)
        self.bound_field = self.form[self.field_name]

        self.heading = self.bound_field.label
        self.help_text = self.bound_field.help_text

    def classes(self):
        classes = super(BaseFieldPanel, self).classes()

        if self.bound_field.field.required:
            classes.append("required")
        if self.bound_field.errors:
            classes.append("error")

        classes.append(self.field_type())
        classes.append("single-field")

        return classes

    def field_type(self):
        return camelcase_to_underscore(self.bound_field.field.__class__.__name__)

    object_template = "wagtailadmin/edit_handlers/single_field_panel.html"

    def render_as_object(self):
        return mark_safe(render_to_string(self.object_template, {
            'self': self,
            'field_content': self.render_as_field(show_help_text=False),
        }))

    field_template = "wagtailadmin/edit_handlers/field_panel_field.html"

    def render_as_field(self, show_help_text=True, extra_context={}):
        context = {
            'field': self.bound_field,
            'field_type': self.field_type(),
            'show_help_text': show_help_text,
        }
        context.update(extra_context)
        return mark_safe(render_to_string(self.field_template, context))

    @classmethod
    def required_fields(self):
        return [self.field_name]


def FieldPanel(field_name, classname="", widget=None):
    base = {
        'field_name': field_name,
        'classname': classname,
    }

    if widget:
        base['widget'] = widget

    return type(str('_FieldPanel'), (BaseFieldPanel,), base)


class BaseRichTextFieldPanel(BaseFieldPanel):
    pass


def RichTextFieldPanel(field_name):
    return type(str('_RichTextFieldPanel'), (BaseRichTextFieldPanel,), {
        'field_name': field_name,
    })


class BaseChooserPanel(BaseFieldPanel):
    """
    Abstract superclass for panels that provide a modal interface for choosing (or creating)
    a database object such as an image, resulting in an ID that is used to populate
    a hidden foreign key input.

    Subclasses provide:
    * field_template
    * object_type_name - something like 'image' which will be used as the var name
      for the object instance in the field_template
    """

    def get_chosen_item(self):
        try:
            return getattr(self.instance, self.field_name)
        except ObjectDoesNotExist:
            # if the ForeignKey is null=False, Django decides to raise
            # a DoesNotExist exception here, rather than returning None
            # like every other unpopulated field type. Yay consistency!
            return None

    def render_as_field(self, show_help_text=True, extra_context={}):
        instance_obj = self.get_chosen_item()
        context = {
            'field': self.bound_field,
            self.object_type_name: instance_obj,
            'is_chosen': bool(instance_obj),
            'show_help_text': show_help_text,
        }
        context.update(extra_context)
        return mark_safe(render_to_string(self.field_template, context))


class BasePageChooserPanel(BaseChooserPanel):
    field_template = "wagtailadmin/edit_handlers/page_chooser_panel.html"
    object_type_name = "page"

    _target_content_type = None

    @classmethod
    def widget_overrides(cls):
        return {cls.field_name: widgets.AdminPageChooser(
            content_type=cls.target_content_type())}

    @classmethod
    def target_content_type(cls):
        if cls._target_content_type is None:
            if cls.page_type:
                try:
                    model = resolve_model_string(cls.page_type)
                except LookupError:
                    raise ImproperlyConfigured("{0}.page_type must be of the form 'app_label.model_name', given {1!r}".format(
                        cls.__name__, cls.page_type))
                except ValueError:
                    raise ImproperlyConfigured("{0}.page_type refers to model {1!r} that has not been installed".format(
                        cls.__name__, cls.page_type))

                cls._target_content_type = ContentType.objects.get_for_model(model)
            else:
                # TODO: infer the content type by introspection on the foreign key
                cls._target_content_type = ContentType.objects.get_by_natural_key('wagtailcore', 'page')

        return cls._target_content_type

    def render_as_field(self, show_help_text=True, extra_context={}):
        context = {
            'choose_another_text_str': ugettext_lazy("Choose another page"),
            'choose_one_text_str': ugettext_lazy("Choose a page"),
        }
        context.update(extra_context)
        return super(BasePageChooserPanel, self).render_as_field(show_help_text, context)


def PageChooserPanel(field_name, page_type=None):
    return type(str('_PageChooserPanel'), (BasePageChooserPanel,), {
        'field_name': field_name,
        'page_type': page_type,
    })


class BaseInlinePanel(EditHandler):
    @classmethod
    def get_panel_definitions(cls):
        # Look for a panels definition in the InlinePanel declaration
        if cls.panels is not None:
            return cls.panels
        # Failing that, get it from the model
        else:
            return extract_panel_definitions_from_model_class(cls.related.model, exclude=[cls.related.field.name])

    _child_edit_handler_class = None

    @classmethod
    def get_child_edit_handler_class(cls):
        if cls._child_edit_handler_class is None:
            panels = cls.get_panel_definitions()
            cls._child_edit_handler_class = MultiFieldPanel(panels, heading=cls.heading)

        return cls._child_edit_handler_class

    @classmethod
    def required_formsets(cls):
        child_edit_handler_class = cls.get_child_edit_handler_class()
        return {
            cls.relation_name: {
                'fields': child_edit_handler_class.required_fields(),
                'widgets': child_edit_handler_class.widget_overrides(),
            }
        }

    def __init__(self, instance=None, form=None):
        super(BaseInlinePanel, self).__init__(instance=instance, form=form)

        self.formset = form.formsets[self.__class__.relation_name]

        child_edit_handler_class = self.__class__.get_child_edit_handler_class()
        self.children = []
        for subform in self.formset.forms:
            # override the DELETE field to have a hidden input
            subform.fields['DELETE'].widget = forms.HiddenInput()

            # ditto for the ORDER field, if present
            if self.formset.can_order:
                subform.fields['ORDER'].widget = forms.HiddenInput()

            self.children.append(
                child_edit_handler_class(instance=subform.instance, form=subform)
            )

        # if this formset is valid, it may have been re-ordered; respect that
        # in case the parent form errored and we need to re-render
        if self.formset.can_order and self.formset.is_valid():
            self.children = sorted(self.children, key=lambda x: x.form.cleaned_data['ORDER'])

        empty_form = self.formset.empty_form
        empty_form.fields['DELETE'].widget = forms.HiddenInput()
        if self.formset.can_order:
            empty_form.fields['ORDER'].widget = forms.HiddenInput()

        self.empty_child = child_edit_handler_class(instance=empty_form.instance, form=empty_form)

    template = "wagtailadmin/edit_handlers/inline_panel.html"

    def render(self):
        formset = render_to_string(self.template, {
            'self': self,
            'can_order': self.formset.can_order,
        })
        js = self.render_js_init()
        return widget_with_script(formset, js)

    js_template = "wagtailadmin/edit_handlers/inline_panel.js"

    def render_js_init(self):
        return mark_safe(render_to_string(self.js_template, {
            'self': self,
            'can_order': self.formset.can_order,
        }))


def InlinePanel(base_model, relation_name, panels=None, label='', help_text=''):
    rel = getattr(base_model, relation_name).related
    return type(str('_InlinePanel'), (BaseInlinePanel,), {
        'relation_name': relation_name,
        'related': rel,
        'panels': panels,
        'heading': label,
        'help_text': help_text,  # TODO: can we pick this out of the foreign key definition as an alternative? (with a bit of help from the inlineformset object, as we do for label/heading)
    })


# This allows users to include the publishing panel in their own per-model override
# without having to write these fields out by hand, potentially losing 'classname'
# and therefore the associated styling of the publishing panel
def PublishingPanel():
    return MultiFieldPanel([
        FieldRowPanel([
            FieldPanel('go_live_at'),
            FieldPanel('expire_at'),
        ], classname="label-above"),
    ], ugettext_lazy('Scheduled publishing'), classname="publishing")


# Now that we've defined EditHandlers, we can set up wagtailcore.Page to have some.
Page.content_panels = [
    FieldPanel('title', classname="full title"),
]

Page.promote_panels = [
    MultiFieldPanel([
        FieldPanel('slug'),
        FieldPanel('seo_title'),
        FieldPanel('show_in_menus'),
        FieldPanel('search_description'),
    ], ugettext_lazy('Common page configuration')),
]

Page.settings_panels = [
    PublishingPanel()
]
