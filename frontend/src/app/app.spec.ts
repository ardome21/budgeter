import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { App } from './app';

describe('App', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [App],
      // The shell renders routerLink directives, which need a router.
      providers: [provideRouter([])],
    }).compileComponents();
  });

  it('should create the app', () => {
    const fixture = TestBed.createComponent(App);
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('renders the primary navigation', () => {
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();
    const links = (fixture.nativeElement as HTMLElement).querySelectorAll(
      'nav a',
    );
    expect(Array.from(links).map((a) => a.textContent?.trim())).toEqual([
      'Month',
      'Transactions',
      'Import',
      'Merchants',
      'Accounts',
    ]);
  });
});
