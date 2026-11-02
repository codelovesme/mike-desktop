"""Turn controls travel with the turn; capability and image refusals keep drafts."""
import base64
import io
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'client'))
from cli import Terminal
import images

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg==')


class Service:
    def __init__(self): self.calls=[]
    def attach(self, *args, **kwargs):
        return {'account':'test@example.invalid','connection':'connected','device_id':'test','chat_version':2}
    def call(self,path,body):
        self.calls.append((path,body))
        if path.endswith('/settings'):
            return {'models':[{'id':'text','vision':False,'thinking':False},{'id':'vision','vision':True,'thinking':True}], 'default_model':'text'}
        return {'turn_id':'test-turn'}


class ChatOptionsTest(unittest.TestCase):
    def make(self):
        service=Service(); terminal=Terminal(service,'/project',True,io.StringIO()); terminal.attach()
        return service, terminal

    def test_model_thinking_mode_are_forwarded(self):
        service,t=self.make()
        t.command('/model vision'); t.command('/think high'); t.command('/mode plan')
        t.ask('describe the plan')
        self.assertEqual(service.calls[-1][1]['model'],'vision')
        self.assertEqual(service.calls[-1][1]['thinking'],'high')
        self.assertEqual(service.calls[-1][1]['mode'],'plan')
        t.command('/mode auto')
        self.assertEqual(service.calls[-1][1]['mode'],'plan','started request was mutated')

    def test_unsupported_choices_are_not_silently_applied(self):
        _,t=self.make()
        for command in ('/model missing','/think high','/mode anything'):
            with self.assertRaises(ValueError): t.command(command)
        self.assertEqual(t.options,{'model':'text','thinking':'off','mode':'ask'})

    def test_queued_message_keeps_its_choices(self):
        _,t=self.make(); t.ask('first'); t.command('/mode plan'); t.ask('queued')
        t.command('/mode auto')
        self.assertEqual(t.waiting[0]['options']['mode'],'plan')

    def test_image_shape_and_damage_are_refused_before_network(self):
        images.validate([images.attachment('image/png',PNG)])
        for value in ([{'mime_type':'image/png','data_base64':'bad'}],
                      [{'mime_type':'image/jpeg','data_base64':base64.b64encode(PNG).decode()}],
                      [images.attachment('image/png',PNG)]*4):
            with self.assertRaises(ValueError): images.validate(value)


if __name__ == '__main__': unittest.main()
